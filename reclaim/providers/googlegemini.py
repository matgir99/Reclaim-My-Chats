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

import contextlib
import re
import time
from types import SimpleNamespace

from ..core import browser, engine, retry
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
    return {status: resp.status, text, retryAfter: resp.headers.get('retry-after')};
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
    with contextlib.suppress(Exception):
        page.goto(APP_URL, wait_until='domcontentloaded', timeout=60000)
    time.sleep(3)
    browser.interactive_login(page, 'accounts.google.com',
                              browser.on_host('gemini.google.com'), 'Google',
                              provider=PROVIDER)


def init_session(page) -> dict:
    """Load /app, extract the fresh tokens, inject the RPC helper."""
    page.goto(APP_URL, wait_until='domcontentloaded', timeout=60000)
    time.sleep(2)
    tokens = extract_tokens(page.content())
    if 'SNlM0e' not in tokens:
        with contextlib.suppress(Exception):
            page.reload(wait_until='domcontentloaded', timeout=60000)
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
    request = {'url': BATCH_URL, 'rpcid': rpcid, 'payload': payload,
               'freq': build_f_req(rpcid, payload), 'at': tokens.get('SNlM0e', ''),
               'bl': tokens.get('cfb2h', ''), 'sid': tokens.get('FdrFJe', ''), 'reqid': reqid}
    for attempt in range(retries):
        response = retry.send(lambda: page.evaluate("(a) => window.__ggRpc(a)", request),
                              what=f'Gemini RPC {rpcid}', retries=retries)
        try:
            err = error_message(decode_response(response['text'], rpcid))
        except Exception as exc:
            raise RuntimeError(f'Gemini RPC {rpcid}: unexpected response ({exc})') from exc
        if err is None:
            return 200, response['text']
        if '1013' in err and attempt < retries - 1:  # batchexecute's "try again"
            time.sleep(2 + attempt * 2)
            continue
        raise RuntimeError(f'RPC {rpcid} error: {err}')
    raise RuntimeError(f'Gemini RPC {rpcid} failed after {retries} attempts')


# ---------------------------------------------------------------------------
# Parsing (pure, fixture-tested; defensive against field drift)
# ---------------------------------------------------------------------------
def _is_list_row(row: list) -> bool:
    """A chat in the chat list: [chatId, title, ...]."""
    return len(row) > 1 and isinstance(row[0], str) and bool(row[0]) and isinstance(row[1], str)


def _is_turn_row(row: list) -> bool:
    """A turn of a chat: [ids, ?, user prompt section, answer candidates, ...]."""
    return (isinstance(row[0], (str, list)) and len(row) > 3
            and isinstance(row[2], list) and isinstance(row[3], list))


def _known_places(data) -> list:
    """Where the rows usually are: data[2], data[0], then data[0][1] and
    data[0][0] (older nested layouts)."""
    if not isinstance(data, list) or not data:
        return []
    places = [data[2]] if len(data) > 2 else []
    places.append(data[0])
    if isinstance(data[0], list):
        places += [data[0][i] for i in (1, 0) if len(data[0]) > i]
    return places


def _lists_within(value):
    """Every list inside `value`, depth first, the outer one first."""
    if isinstance(value, list):
        yield value
        for item in value:
            yield from _lists_within(item)


def _find_rows(data, want_user: bool = False):
    """Locate the rows/turns array in a decoded batchexecute payload.

    Looks in the known places first, then everywhere, and returns the array
    with the most elements shaped like the wanted rows (the first one found
    on a tie), or [] when none is.
    """
    looks_like = _is_turn_row if want_user else _is_list_row
    best, best_count = [], 0
    for place in [*_known_places(data), data]:
        for candidate in _lists_within(place):
            count = sum(1 for row in candidate if isinstance(row, list) and row and looks_like(row))
            if count > best_count:
                best, best_count = candidate, count
    return best


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


def _unwrap(value) -> str:
    """The text inside list wrappers: ['...'] or [['...']]."""
    while isinstance(value, list) and value:
        value = value[0]
    return value if isinstance(value, str) else ''


def _turn_time(row):
    """A turn's epoch seconds, when the row carries them (at [1] or [4])."""
    for index in (1, 4):
        if len(row) > index and isinstance(row[index], list) and row[index]:
            value = row[index][0]
            if isinstance(value, (int, float)) and value > 1e9:
                return value
    return None


def _oldest_first(rows: list) -> list:
    """Turns in reading order: by time when every turn has one; otherwise the
    current API, whose ids are [conversation_id, reply_id], lists the newest
    first."""
    if rows and all(_turn_time(row) is not None for row in rows):
        return sorted(rows, key=lambda row: _turn_time(row) or 0)
    if rows and all(isinstance(row[0], list) for row in rows):
        return list(reversed(rows))
    return rows


def _turn_entries(turn: list) -> list[dict]:
    """A turn's user prompt and the model's answer (its first candidate)."""
    user_text = _unwrap(turn[2][0]) if isinstance(turn[2], list) and turn[2] else ''
    answers = turn[3]
    candidate = (answers[0][0] if isinstance(answers, list) and answers
                 and isinstance(answers[0], list) and answers[0] else None)
    model_text, thought = '', False
    if isinstance(candidate, list):
        model_text = _unwrap(candidate[1]) if len(candidate) > 1 else ''
        # the thoughts section: candidate[37], in some captures turn[37]
        thought = bool(candidate[37]) if len(candidate) > 37 else (
            len(turn) > 37 and bool(turn[37]))
    entries = []
    if user_text.strip():
        entries.append({'role': 'user', 'text': user_text, 'thought': False})
    if model_text.strip():
        entries.append({'role': 'model', 'text': model_text,
                        'thought': False, 'had_thoughts': thought})
    elif thought:
        entries.append({'role': 'model', 'text': '', 'thought': True})
    return entries


def parse_read(data) -> list[dict]:
    """hNvQHb result -> [{role, text, thought}] pairs.

    Turn shape (per research): turn[2] = user prompt section with text at
    [0]; turn[3] = [[candidate, ...]] with text at candidate[1] and the
    thoughts section at candidate[37] (thinking models). Texts may be
    list-wrapped (['...']); unwrapped defensively. Citations
    (candidate[2]) and generated/uploaded images are v1 gaps.
    """
    out = []
    for turn in _oldest_first(_find_rows(data, want_user=True)):
        if isinstance(turn, list) and len(turn) >= 4:
            out += _turn_entries(turn)
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
    """Every chat through the list RPC, paged with its cursor. Returns the
    chats and the next request id (Gemini expects increasing ids)."""
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
    """One chat through the read RPC: (chat, raw payload, next request id).
    A reply with MAX_TURNS turns may be cut short, so it is refused rather
    than saved incomplete."""
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
