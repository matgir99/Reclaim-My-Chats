"""Google Gemini provider (scrape mode) — batchexecute RPC replay.

Replays the web app's own BardChatUi RPCs from page context, exactly like
the app: tokens are regex-extracted from the /app HTML ("SNlM0e" -> form
field `at`, "cfb2h" -> `bl` param, "FdrFJe" -> `f.sid` param) and rotate
with every deploy, so they are re-extracted each run.

  POST https://gemini.google.com/_/BardChatUi/data/batchexecute
  RPCs: MaZiqc (list, payload [200]) · hNvQHb (read)

Shapes per research (EricAndrechek/gemini-usage, HanaokaYuzu/Gemini-API;
see docs/status_and_plan/GEMINI_CLAUDE_PLAN.md). Field positions WILL
drift — parsers here are defensive (documented candidate paths + a
recursive fallback) and live recon must confirm. Generated/uploaded
images and citations are v1 gaps (documented below).
"""

from __future__ import annotations

import argparse
import re
import time
import traceback
from pathlib import Path

from ..core import browser, config
from ..core.batchexecute import build_f_req, decode_response, error_message
from ..core.manifest import SyncState, print_dry_run, write_manifest
from ..core.model import Chat, Turn
from ..core.progress import progress
from ..core.writer import write_chat, write_raw

APP_URL = 'https://gemini.google.com/app'
BATCH_URL = 'https://gemini.google.com/_/BardChatUi/data/batchexecute'
PROVIDER = 'googlegemini'

LIST_RPC = 'MaZiqc'
LIST_PAYLOAD = [200]
READ_RPC = 'hNvQHb'
MAX_TURNS = 100  # v1: fixed window; very long chats may need pagination (recon)

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
                              '**/gemini.google.com/**', 'Google')


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
            decoded = decode_response(r['text'])
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

    Documented candidate paths first (data[0][1], then data[0][0]),
    then a recursive search for the deepest array whose elements look
    like the expected rows. Returns the best candidate or [].
    """
    candidates = []
    if isinstance(data, list) and data and isinstance(data[0], list):
        for i in (1, 0):
            if len(data[0]) > i:
                candidates.append(data[0][i])

    def score(cand):
        if not isinstance(cand, list) or not cand:
            return -1
        n_ok = 0
        for el in cand:
            if not isinstance(el, list) or not el or not isinstance(el[0], str):
                continue
            if want_user:
                # read rows: turn[2] user prompt array + turn[3] candidates
                if (len(el) > 3 and isinstance(el[2], list)
                        and isinstance(el[3], list)):
                    n_ok += 1
            else:
                # list rows: [chatId, title, ...]
                if len(el) > 1:
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

    for c in candidates:
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

    Turn shape (per research): turn[2] = [user prompt text, ...];
    turn[3] = [[candidate, ...]] with text at candidate[1]; thought flag
    at turn[37] (thinking models). Citations (candidate[2]) and
    generated/uploaded images are v1 gaps.
    """
    out = []
    for t in _find_rows(data, want_user=True):
        if not isinstance(t, list) or len(t) < 4:
            continue
        user_text = ''
        if isinstance(t[2], list) and t[2] and isinstance(t[2][0], str):
            user_text = t[2][0]
        model_text = ''
        cand = None
        if isinstance(t[3], list) and t[3] and isinstance(t[3][0], list) \
                and t[3][0]:
            cand = t[3][0][0]
        if isinstance(cand, list) and len(cand) > 1 and isinstance(cand[1], str):
            model_text = cand[1]
        thought = bool(t[37]) if len(t) > 37 else False
        if user_text.strip():
            out.append({'role': 'user', 'text': user_text, 'thought': False})
        if model_text.strip():
            out.append({'role': 'model', 'text': model_text, 'thought': thought})
    return out


def read_to_chat(parsed: list[dict], chat_id: str, source_url: str,
                 title: str = '') -> Chat:
    turns = [Turn(role=e['role'], text=e['text'], thought=e['thought'])
             for e in parsed]
    return Chat(id=chat_id, title=title or '(untitled)',
                source_url=source_url, turns=turns, provider=PROVIDER)


# ---------------------------------------------------------------------------
# Listing + fetching
# ---------------------------------------------------------------------------
def list_chats(page, tokens: dict, reqid: int) -> tuple[list[dict], int]:
    status, text = rpc_call(page, tokens, LIST_RPC, LIST_PAYLOAD, reqid)
    if status != 200:
        raise RuntimeError(f'{LIST_RPC} HTTP {status}')
    chats = parse_list(decode_response(text))
    if not chats:
        raise RuntimeError(f'{LIST_RPC} returned no chats (shape changed?)')
    return chats, reqid + 1


def fetch_chat(page, tokens: dict, chat_id: str, reqid: int,
               title: str = '') -> tuple[Chat, list, int]:
    payload = [chat_id, MAX_TURNS, None, 1, [0], [4], None, 1]
    status, text = rpc_call(page, tokens, READ_RPC, payload, reqid)
    if status != 200:
        raise RuntimeError(f'{READ_RPC} HTTP {status}')
    raw = decode_response(text)
    parsed = parse_read(raw)
    chat = read_to_chat(parsed, chat_id,
                        f'https://gemini.google.com/app/{chat_id}', title)
    return chat, raw, reqid + 1


def run(page, tokens: dict, chats: list[dict], out_dir: Path,
        skip_unchanged: bool = False, updated_map: dict | None = None,
        save_raw: bool = True, log: bool = False) -> list[dict]:
    """Fetch the given chats. Returns per-chat result dicts for the manifest.

    skip_unchanged: skip chats whose server updated_at matches the local
    sync record (the default update mode). title/--url/--rebuild
    selections pass False so matches are always fetched freshly. Per-chat
    lines print only with log=True (failures always print).
    """
    out_dir = Path(out_dir)
    sync = SyncState(out_dir, PROVIDER)
    updated_map = updated_map or {}
    results = []
    t_run = time.time()
    reqid = 100000
    for i, chat in enumerate(chats):
        label = (chat.get('title') or chat['id'])[:55]
        if log:
            print(progress(i + 1, len(chats), t_run))
        if skip_unchanged and sync.is_unchanged(chat['id'],
                                                updated_map.get(chat['id'])):
            if log:
                print(f'[{i + 1}/{len(chats)}] {label} -> skip (unchanged)')
            continue
        t0 = time.time()
        try:
            canonical, raw, reqid = fetch_chat(page, tokens, chat['id'],
                                               reqid, chat.get('title', ''))
            stats = write_chat(canonical, out_dir)
            if save_raw:
                try:
                    write_raw(stats['dir'], raw)
                except Exception:
                    pass
            sync.mark(chat['id'], updated_map.get(chat['id']), str(stats['md']))
            results.append({'id': chat['id'], 'title': canonical.title,
                            'ok': True,
                            'duration_s': round(time.time() - t0, 1),
                            **{k: stats[k] for k in ('turns', 'chars', 'images', 'docs')}})
            if log:
                print(f'[{i + 1}/{len(chats)}] {label} -> {stats["turns"]}t, '
                      f'{stats["chars"]:,} chars · {time.time() - t0:.1f}s')
        except Exception as e:
            print(f'[{i + 1}/{len(chats)}] {label} -> FAILED: {e}')
            traceback.print_exc()
            results.append({'id': chat['id'], 'title': label, 'ok': False,
                            'error': str(e)})
        time.sleep(0.4)
    sync.save()
    return results


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog='reclaim googlegemini',
        description='Archive Google Gemini chats (update by default).')
    ap.add_argument('title', nargs='?',
                    help='fetch chats whose title contains this '
                         '(case-insensitive)')
    ap.add_argument('--rebuild', action='store_true',
                    help='fetch everything, overwriting local copies')
    ap.add_argument('--url', help='fetch one exact chat URL')
    ap.add_argument('--list', action='store_true',
                    help='print chat titles, no download')
    ap.add_argument('--log', action='store_true',
                    help='full per-chat log + verbose progress and timings')
    ap.add_argument('--dry-run', action='store_true',
                    help='preview what would be fetched; nothing downloaded')
    ap.add_argument('--skip', type=int, default=0,
                    help='skip the first N chats in the listing')
    ap.add_argument('--limit', type=int, default=0,
                    help='fetch at most N chats')
    ap.add_argument('--no-raw', action='store_true',
                    help='do not save media-stripped raw.json per chat')
    ap.add_argument('-o', '--output-dir',
                    default=str(config.archive_root() / 'Google Gemini'))
    return ap


def parse_args(argv=None):
    """Parse + validate CLI args (shared by main() and `reclaim all`)."""
    ap = build_parser()
    args = ap.parse_args(argv)
    if args.title and args.url:
        ap.error('pass a title or --url, not both')
    return args


def main(argv=None):
    args = parse_args(argv)
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        ctx, page = browser.launch(p)
        try:
            return run_session(page, args)
        finally:
            ctx.close()


def run_session(page, args) -> int:
    """Provider session on an already-launched page; shared by main()
    (own browser) and `reclaim all` (one browser for all providers)."""
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    started = time.time()
    ensure_logged_in(page)
    tokens = init_session(page)
    if args.url:
        cid = args.url.rstrip('/').split('/')[-1].split('?')[0]
        chats = [{'id': cid, 'title': ''}]
        updated_map = {}
    else:
        print('Listing chats...')
        chats, _ = list_chats(page, tokens, 100000)
        updated_map = {c['id']: c.get('updated_at') for c in chats}
        print(f'Found {len(chats)} chats')
    if args.title:
        chats = [c for c in chats
                 if args.title.lower() in (c.get('title') or '').lower()]
    if args.skip:
        chats = chats[args.skip:]
    if args.limit:
        chats = chats[:args.limit]
    if not chats:
        print('No chats matched.')
        return 1
    if args.list:
        for n, c in enumerate(chats, 1):
            print(f'{n}. {c.get("title") or c["id"]}')
        print(f'-- {len(chats)} chat(s)')
        return 0
    skip_unchanged = not args.rebuild and not args.title
    if args.dry_run:
        sync = SyncState(out_dir, PROVIDER, migrate=False)
        return print_dry_run(chats, updated_map, sync, skip_unchanged,
                             log=args.log)

    print(f"\n{'=' * 50}\n  {len(chats)} chats\n{'=' * 50}\n")
    results = run(page, tokens, chats, out_dir, skip_unchanged=skip_unchanged,
                  updated_map=updated_map, save_raw=not args.no_raw,
                  log=args.log)
    manifest = write_manifest(out_dir, PROVIDER, results, started)
    ok = sum(1 for r in results if r.get('ok'))
    print(f"\n{'=' * 50}")
    print(f'  Done: {ok} ok, {len(results) - ok} failed')
    print(f'  Manifest: {manifest}')
    print(f"{'=' * 50}")
    return 0 if ok == len(results) else 2


if __name__ == '__main__':
    raise SystemExit(main())
