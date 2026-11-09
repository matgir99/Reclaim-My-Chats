"""Google Gemini provider (scrape mode) — batchexecute RPC replay.

Replays the web app's own BardChatUi RPCs from page context, exactly like
the app: tokens are regex-extracted from the /app HTML ("SNlM0e" -> form
field `at`, "cfb2h" -> `bl` param, "FdrFJe" -> `f.sid` param) and rotate
with every deploy, so they are re-extracted each run.

  POST https://gemini.google.com/_/BardChatUi/data/batchexecute
  RPCs: MaZiqc (cursor-paginated list) · hNvQHb (read)

Shapes per MIT references Kept and EricAndrechek/gemini-usage;
see docs/UPSTREAM_REVIEW.md. Field positions WILL
drift — parsers here are defensive (documented candidate paths + a
recursive fallback) and live recon must confirm. Generated/uploaded
images and citations are v1 gaps (documented below).
"""

from __future__ import annotations

import re
import time
from types import SimpleNamespace

from ..core import browser, engine
from ..core.events import EventSink
from ..core.batchexecute import build_f_req, decode_response, error_message
from ..core.model import Chat, Turn

APP_URL = 'https://gemini.google.com/app'
BATCH_URL = 'https://gemini.google.com/_/BardChatUi/data/batchexecute'
PROVIDER = 'googlegemini'

LIST_RPC = 'MaZiqc'
LIST_PAGE_SIZE = 100
READ_RPC = 'hNvQHb'
MAX_TURNS = 10000  # upstream-supported window; refuse a response that fills it

RPC_JS = """window.__ggRpc = async function(a) {
    const form = new URLSearchParams();
    form.set('at', a.at);
    form.set('f.req', a.freq);
    let q = 'rpcids=' + encodeURIComponent(a.rpcid) + '&_reqid=' + a.reqid +
            '&rt=c&source-path=/app&bl=' + encodeURIComponent(a.bl);
    if (a.sid) q += '&f.sid=' + encodeURIComponent(a.sid);
    const resp = await fetch(a.url + '?' + q, {
        method: 'POST', credentials: 'include',
        headers: {'content-type': 'application/x-www-form-urlencoded;charset=UTF-8'},
        body: form.toString(),
    });
    const text = await resp.text();
    return {status: resp.status, text};
}; 'ready'"""


# ---------------------------------------------------------------------------
# Session + tokens
# ---------------------------------------------------------------------------
_TOKEN_PATTERNS = (
    ('SNlM0e', r'"SNlM0e":"([^"]+)"'),   # form field `at`
    ('cfb2h', r'"cfb2h":"([^"]+)"'),     # `bl` (build label)
    ('FdrFJe', r'"FdrFJe":"([^"]+)"'),   # `f.sid` (session id)
)


def extract_tokens(html: str) -> dict:
    """The app's embedded tokens; they rotate with every deploy."""
    out = {}
    for name, pat in _TOKEN_PATTERNS:
        m = re.search(pat, html)
        if m:
            out[name] = m.group(1)
    return out


def ensure_logged_in(page):
    try:
        page.goto(APP_URL, wait_until='domcontentloaded', timeout=60000)
    except Exception:
        pass
    time.sleep(3)
    browser.interactive_login(page, 'accounts.google.com',
                              browser.on_host('gemini.google.com'), 'Google')


def init_session(page) -> dict:
    """Load /app, extract the fresh tokens, inject the RPC helper."""
    page.goto(APP_URL, wait_until='domcontentloaded', timeout=60000)
    time.sleep(2)
    tokens = extract_tokens(page.content())
    if 'SNlM0e' not in tokens:
        try:
            page.reload(wait_until='domcontentloaded', timeout=60000)
        except Exception:
            pass
        time.sleep(3)
        tokens = extract_tokens(page.content())
    if 'SNlM0e' not in tokens:
        raise RuntimeError('no SNlM0e token in /app HTML (shape changed '
                           'or not logged in?)')
    page.evaluate(RPC_JS)
    return tokens


def rpc_call(page, tokens: dict, rpcid: str, payload, reqid: int,
             retries: int = 3) -> tuple[int, str]:
    """One batchexecute call. Retries 1013 (transient); raises on
    quota/rate-limit codes and HTTP auth failures."""
    last_err = None
    for attempt in range(retries):
        try:
            r = page.evaluate(
                "(a) => window.__ggRpc(a)",
                {'url': BATCH_URL, 'rpcid': rpcid,
                 'payload': payload,
                 'freq': build_f_req(rpcid, payload),
                 'at': tokens.get('SNlM0e', ''),
                 'bl': tokens.get('cfb2h', ''),
                 'sid': tokens.get('FdrFJe', ''),
                 'reqid': reqid})
            if r['status'] != 200:
                if r['status'] in (401, 403):
                    raise RuntimeError(f'auth failed ({r["status"]}) — '
                                       'session expired?')
                last_err = f'HTTP {r["status"]}: {r["text"][:120]}'
                time.sleep(2 + attempt * 2)
                continue
            decoded = decode_response(r['text'], rpcid)
            err = error_message(decoded)
            if err is None:
                return 200, r['text']
            if '1013' in err and attempt < retries - 1:
                time.sleep(2 + attempt * 2)
                continue
            raise RuntimeError(f'RPC {rpcid} error: {err}')
        except RuntimeError:
            raise
        except Exception as e:
            last_err = e
            time.sleep(2 + attempt * 2)
    raise RuntimeError(f'RPC {rpcid} failed after {retries} attempts: {last_err}')


# ---------------------------------------------------------------------------
# Parsing (pure, fixture-tested; defensive against field drift)
# ---------------------------------------------------------------------------
def _find_rows(data, want_user: bool = False):
    """Locate the rows/turns array in a decoded batchexecute payload.

    Documented candidate paths first (data[2], data[0], legacy nested rows),
    then a recursive search for the deepest array whose elements look
    like the expected rows. Returns the best candidate or [].
    """
    candidates = []
    if isinstance(data, list):
        if len(data) > 2:
            candidates.append(data[2])
        if data:
            candidates.append(data[0])
    if isinstance(data, list) and data and isinstance(data[0], list):
        for i in (1, 0):
            if len(data[0]) > i:
                candidates.append(data[0][i])

    def score(cand):
        if not isinstance(cand, list) or not cand:
            return -1
        n_ok = 0
        for el in cand:
            if not isinstance(el, list) or not el:
                continue
            if want_user:
                # read rows: turn[2] user prompt array + turn[3] candidates
                if (isinstance(el[0], (str, list)) and len(el) > 3
                        and isinstance(el[2], list)
                        and isinstance(el[3], list)):
                    n_ok += 1
            else:
                # list rows: [chatId, title, ...]
                if (len(el) > 1 and isinstance(el[0], str) and el[0]
                        and isinstance(el[1], str)):
                    n_ok += 1
        return n_ok

    best, best_n = None, 0

    def walk(o):
        nonlocal best, best_n
        if isinstance(o, list):
            n = score(o)
            if n > best_n:
                best, best_n = o, n
            for el in o:
                walk(el)

    for c in [*candidates, data]:
        walk(c)
    return best if best is not None else []


def parse_list(data) -> list[dict]:
    """MaZiqc result -> [{'id', 'title', 'updated_at'}].

    Row shape (per research): [chatId, title, ..., [epoch_s, nanos] at
    index 5, ...]. updated_at is epoch seconds (int) — SyncState
    compares it as a string, so int vs int is fine.
    """
    out = []
    for row in _find_rows(data):
        cid = row[0]
        if not isinstance(cid, str) or not cid:
            continue
        title = row[1] if len(row) > 1 and isinstance(row[1], str) else ''
        updated = None
        if len(row) > 5 and isinstance(row[5], list) and row[5]:
            v = row[5][0]
            if isinstance(v, (int, float)):
                updated = v
            elif isinstance(v, list) and v and isinstance(v[0], (int, float)):
                updated = v[0]
        out.append({'id': cid, 'title': title, 'updated_at': updated})
    return out


def parse_read(data) -> list[dict]:
    """hNvQHb result -> [{role, text, thought}] pairs.

    Turn shape (per research): turn[2] = user prompt section with text at
    [0]; turn[3] = [[candidate, ...]] with text at candidate[1] and the
    thoughts section at candidate[37] (thinking models). Texts may be
    list-wrapped (['...']); unwrapped defensively. Citations
    (candidate[2]) and generated/uploaded images are v1 gaps.
    """
    def _unwrap(v) -> str:
        while isinstance(v, list) and v:
            v = v[0]
        return v if isinstance(v, str) else ''

    rows = _find_rows(data, want_user=True)
    def timestamp(row):
        for index in (1, 4):
            if len(row) > index and isinstance(row[index], list) and row[index]:
                value = row[index][0]
                if isinstance(value, (int, float)) and value > 1e9:
                    return value
        return None

    if rows and all(timestamp(row) is not None for row in rows):
        rows = sorted(rows, key=lambda row: timestamp(row) or 0)
    elif rows and all(isinstance(row[0], list) for row in rows):
        rows = list(reversed(rows))  # current API metadata [conversation_id, reply_id]
    out = []
    for t in rows:
        if not isinstance(t, list) or len(t) < 4:
            continue
        user_text = _unwrap(t[2][0]) if isinstance(t[2], list) and t[2] else ''
        cand = None
        if isinstance(t[3], list) and t[3] and isinstance(t[3][0], list) \
                and t[3][0]:
            cand = t[3][0][0]
        model_text = ''
        thought = False
        if isinstance(cand, list):
            if len(cand) > 1:
                model_text = _unwrap(cand[1])
            if len(cand) > 37:
                thought = bool(cand[37])
            elif len(t) > 37:
                thought = bool(t[37])
        if user_text.strip():
            out.append({'role': 'user', 'text': user_text, 'thought': False})
        if model_text.strip():
            out.append({'role': 'model', 'text': model_text,
                        'thought': False, 'had_thoughts': thought})
        elif thought:
            out.append({'role': 'model', 'text': '', 'thought': True})
    return out


def read_to_chat(parsed: list[dict], chat_id: str, source_url: str,
                 title: str = '') -> Chat:
    turns = [Turn(role=e['role'], text=e['text'], thought=e['thought'],
                  had_thoughts=e.get('had_thoughts', False))
             for e in parsed]
    return Chat(id=chat_id, title=title or '(untitled)',
                source_url=source_url, turns=turns, provider=PROVIDER)


# ---------------------------------------------------------------------------
# Listing + fetching
# ---------------------------------------------------------------------------
def list_chats(page, tokens: dict, reqid: int) -> tuple[list[dict], int]:
    chats, cursor = [], None
    seen_ids, seen_cursors = set(), set()
    while True:
        payload = [LIST_PAGE_SIZE, cursor, [0, None, 1]]
        status, text = rpc_call(page, tokens, LIST_RPC, payload, reqid)
        reqid += 1
        if status != 200:
            raise RuntimeError(f'{LIST_RPC} HTTP {status}')
        data = decode_response(text, LIST_RPC)
        batch = parse_list(data)
        for item in batch:
            if item['id'] not in seen_ids:
                seen_ids.add(item['id'])
                chats.append(item)
        if not batch and data != [] and not any(
                len(data) > index and data[index] == [] for index in (0, 2)):
            raise RuntimeError(f'{LIST_RPC} returned an unknown conversation list shape')
        cursor = data[1] if len(data) > 1 and isinstance(data[1], str) else None
        if not cursor:
            return chats, reqid
        if cursor in seen_cursors:
            raise RuntimeError('Gemini returned a repeated conversation cursor')
        seen_cursors.add(cursor)


def fetch_chat(page, tokens: dict, chat_id: str, reqid: int,
               title: str = '') -> tuple[Chat, list, int]:
    payload = [chat_id, MAX_TURNS]
    status, text = rpc_call(page, tokens, READ_RPC, payload, reqid)
    if status != 200:
        raise RuntimeError(f'{READ_RPC} HTTP {status}')
    raw = decode_response(text, READ_RPC)
    if len(_find_rows(raw, want_user=True)) >= MAX_TURNS:
        raise RuntimeError('Gemini message history may be truncated; refusing to save an incomplete chat')
    parsed = parse_read(raw)
    if not parsed:
        raise RuntimeError('Gemini returned no readable messages; response shape may have changed')
    chat = read_to_chat(parsed, chat_id,
                        f'https://gemini.google.com/app/{chat_id}', title)
    return chat, raw, reqid + 1


class GoogleGemini(engine.Provider):
    """What only Gemini does; the engine does the rest."""

    name = PROVIDER
    folder = 'Google Gemini'
    description = 'Save Google Gemini chats to the database (update by default).'
    pace = 0.4

    def open(self, page):
        ensure_logged_in(page)
        # Each request carries a counter that keeps increasing during a session.
        return SimpleNamespace(page=page, tokens=init_session(page), reqid=100000)

    def listing(self, s) -> list[engine.ChatRef]:
        chats, s.reqid = list_chats(s.page, s.tokens, s.reqid)
        return [engine.ChatRef(id=c['id'], title=c.get('title', ''),
                               updated_at=c.get('updated_at'), data=c) for c in chats]

    def from_url(self, s, url: str) -> engine.ChatRef:
        return engine.ChatRef(id=url.rstrip('/').split('/')[-1].split('?')[0])

    def fetch(self, s, ref: engine.ChatRef) -> engine.Fetched:
        chat, raw, s.reqid = fetch_chat(s.page, s.tokens, ref.id, s.reqid, ref.title)
        return engine.Fetched(chat, raw=raw)


SPEC = GoogleGemini()


def build_parser():
    return engine.build_parser(SPEC)


def parse_args(argv=None):
    return engine.parse_args(SPEC, argv)


def run_session(page, args, sink: EventSink | None = None) -> int:
    """This provider on an already opened page (window and command line)."""
    return engine.run_session(SPEC, page, args, sink)


def main(argv=None):
    return engine.main(SPEC, argv)


if __name__ == '__main__':
    raise SystemExit(main())
