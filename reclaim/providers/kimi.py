"""Kimi (kimi.com) provider (scrape mode) — native API replay.

Endpoints and auth learned from Kept's MIT-licensed adapter
(docs/providers/kimi.md). The session Bearer token lives in kimi.com's
localStorage; we read it after login and replay three Connect-style JSON
endpoints:

  POST /apiv2/kimi.chat.v1.ChatService/ListChats      {project_id:"", page_size, query:"", page_token?}
  POST /apiv2/kimi.chat.v1.ChatService/GetChat        {chat_id}
  POST /apiv2/kimi.gateway.chat.v1.ChatService/ListMessages {chat_id, page_size}

ListMessages returns messages NEWEST-first (reversed here). Content is a
string in msg.content / msg.text, or block-structured in msg.blocks[]
(block.text.content for TEXT blocks; blocks whose type hints THINK/REASON
become thought turns and are omitted from the archive).
"""

from __future__ import annotations

import argparse
import json
import re
import time
import traceback
from pathlib import Path

from ..core import browser
from ..core.manifest import SyncState, print_dry_run, write_manifest
from ..core.model import Chat, Turn
from ..core.progress import progress
from ..core.writer import write_chat, write_raw

BASE_URL = 'https://www.kimi.com'
API = 'https://www.kimi.com/apiv2'
PROVIDER = 'kimi'
PAGE_SIZE = 50
MSG_PAGE_SIZE = 200
_THINK_HINT = re.compile(r'think|reason', re.I)

_FETCH_JS = """async (args) => {
    const resp = await fetch(args.url, {
        method: 'POST',
        headers: {'content-type': 'application/json', 'accept': 'application/json',
                  'authorization': 'Bearer ' + args.token},
        body: JSON.stringify(args.body),
        credentials: 'include',
    });
    const text = await resp.text();
    return {status: resp.status, text};
}"""


def _api(page, token: str, path: str, body: dict, retries: int = 3) -> dict:
    last = None
    for attempt in range(retries):
        try:
            r = page.evaluate(_FETCH_JS, {'url': f'{API}/{path}',
                                          'token': token, 'body': body})
            if r['status'] == 200:
                return json.loads(r['text'])
            if r['status'] in (401, 403):
                raise RuntimeError(f'auth failed ({r["status"]}) — token expired?')
            last = f'HTTP {r["status"]}: {r["text"][:120]}'
        except RuntimeError:
            raise
        except Exception as e:
            last = e
        time.sleep(1.5 + attempt)
    raise RuntimeError(f'{path} failed: {last}')


def get_token(page) -> str:
    """Find the session token in kimi.com storage and validate it."""
    inventory = page.evaluate("""() => {
        const out = [];
        for (const store of [localStorage, sessionStorage]) {
            for (let i = 0; i < store.length; i++) {
                const k = store.key(i);
                if (/anonymous|guest/i.test(k)) continue;
                const v = store.getItem(k) || '';
                out.push({key: (store === localStorage ? 'L:' : 'S:') + k,
                          len: v.length,
                          jwt: v.startsWith('eyJ'),
                          val: v});  // full value — truncation breaks validation!
            }
        }
        return out;
    }""")
    tried = 0
    for cand in inventory:
        token = cand['val'].strip().strip('"')
        # heuristic: tokens are long, space-free, often JWTs
        if len(token) < 30 or ' ' in token or '\n' in token:
            continue
        if not (cand['jwt'] or re.search(r'token|access|jwt|session|auth',
                                         cand['key'], re.I)):
            continue
        tried += 1
        try:
            _api(page, token, 'kimi.chat.v1.ChatService/ListChats',
                 {'project_id': '', 'page_size': 1, 'query': ''}, retries=1)
            print(f"  token found ({cand['key']})")
            return token
        except Exception:
            continue
    keys = ', '.join(f"{c['key']}[{c['len']}]" for c in inventory)
    raise RuntimeError(
        f'no valid Kimi token (tried {tried} candidates). '
        f'storage keys: {keys[:600]}')


def ensure_logged_in(page):
    try:
        page.goto(BASE_URL, wait_until='domcontentloaded', timeout=60000)
    except Exception:
        pass  # navigation quirks are fine; token probe decides
    time.sleep(4)
    try:
        return get_token(page)
    except Exception:
        pass
    # Kimi login is an SPA modal (no URL change): show the window and poll
    # until a valid token appears in localStorage.
    page.bring_to_front()
    browser.set_window_bounds(page, state='normal', left=100, top=100)
    print('\n' + '=' * 50)
    print('  LOG IN to Kimi in the browser window.')
    print('  Waiting (up to 15 minutes)...')
    print('=' * 50 + '\n')
    import os
    deadline = time.time() + int(os.environ.get('RECLAIM_LOGIN_TIMEOUT', '900'))
    while time.time() < deadline:
        try:
            token = get_token(page)
            print('Logged in! Hiding window...\n')
            browser.set_window_bounds(page, state='minimized')
            time.sleep(2)
            return token
        except Exception:
            time.sleep(3)
    raise RuntimeError('Login timeout')


def list_chats(page, token: str) -> list[dict]:
    chats, page_token = [], ''
    while True:
        body = {'project_id': '', 'page_size': PAGE_SIZE, 'query': ''}
        if page_token:
            body['page_token'] = page_token
        data = _api(page, token, 'kimi.chat.v1.ChatService/ListChats', body)
        chats.extend(data.get('chats') or [])
        page_token = data.get('nextPageToken') or ''
        if not page_token:
            return chats


def list_messages(page, token: str, chat_id: str) -> list[dict]:
    data = _api(page, token, 'kimi.gateway.chat.v1.ChatService/ListMessages',
                {'chat_id': chat_id, 'page_size': MSG_PAGE_SIZE})
    msgs = data.get('messages') or data.get('items') or []
    return list(reversed(msgs))  # API returns newest-first


def _msg_to_turn(msg: dict) -> tuple[Turn | None, list[str]]:
    """Convert one API message. Returns (turn, [image urls])."""
    role_raw = (msg.get('role') or msg.get('sender') or '').lower()
    role = 'user' if role_raw in ('user', 'human') else 'model'
    if role_raw == 'system':
        return None, []

    texts, images, thought = [], [], False
    content = msg.get('content')
    if isinstance(content, str):
        texts.append(content)
    elif isinstance(msg.get('text'), str):
        texts.append(msg['text'])
    for block in msg.get('blocks', []) or []:
        if not isinstance(block, dict):
            continue
        btype = str(block.get('type') or '')
        if _THINK_HINT.search(btype):
            thought = True
            continue
        text = (block.get('text') or {}).get('content') or block.get('content') or ''
        if isinstance(text, str) and text:
            texts.append(text)
        # best-effort image blocks (url or base64 payloads, various shapes)
        for key in ('image', 'image_url', 'img', 'url'):
            v = block.get(key)
            if isinstance(v, dict) and isinstance(v.get('url'), str):
                images.append(v['url'])
            elif isinstance(v, str) and v.startswith('http'):
                images.append(v)

    text = '\n'.join(t for t in texts if t and t.strip()).strip()
    if not text and not images:
        return None, []
    return Turn(role=role, text=text, thought=thought), images


def parse_chat(meta: dict, messages: list[dict]) -> tuple[Chat, dict]:
    """Returns (chat, image_map) where image_map maps id(turn) -> [urls]."""
    chat_id = meta.get('id') or meta.get('chat_id') or ''
    title = meta.get('name') or meta.get('title') or ''
    turns: list[Turn] = []
    image_map: dict[int, list[str]] = {}
    for m in messages:
        t, urls = _msg_to_turn(m)
        if t is None:
            continue
        if urls:
            image_map[id(t)] = urls
        turns.append(t)
    if not title and turns:
        first_user = next((t for t in turns if t.role == 'user'), None)
        title = ((first_user.text[:80].replace('\n', ' ') if first_user else '')
                 or '(untitled)')
    chat = Chat(id=chat_id, title=title,
                source_url=f'{BASE_URL}/chat/{chat_id}',
                turns=turns, provider=PROVIDER)
    return chat, image_map


def _materialize_images(page, chat: Chat, image_map: dict) -> int:
    ok = 0
    for turn in chat.turns:
        for u in image_map.get(id(turn), []):
            try:
                resp = page.request.get(u, timeout=60000)
                if resp.status == 200 and resp.body():
                    turn.images.append(resp.body())
                    ok += 1
            except Exception:
                pass
    return ok


def run(page, token: str, chats: list[dict], out_dir: Path,
        skip_unchanged: bool = False, save_raw: bool = True,
        log: bool = False) -> list[dict]:
    """Fetch the given chats. Returns per-chat results for the manifest."""
    out_dir = Path(out_dir)
    sync = SyncState(out_dir, PROVIDER)
    results = []
    t_run = time.time()
    for i, item in enumerate(chats):
        chat_id = item.get('id') or item.get('chat_id') or ''
        label = (item.get('name') or item.get('title') or chat_id)[:55]
        updated = item.get('updateTime') or item.get('updated_at')
        if log:
            print(progress(i + 1, len(chats), t_run))
        if skip_unchanged and sync.is_unchanged(chat_id, updated):
            print(f'[{i + 1}/{len(chats)}] {label} -> skip (unchanged)')
            continue
        t0 = time.time()
        try:
            meta = item
            if not (meta.get('name') or meta.get('title')):
                try:
                    d = _api(page, token, 'kimi.chat.v1.ChatService/GetChat',
                             {'chat_id': chat_id}, retries=1)
                    meta = d.get('chat') or d
                except Exception:
                    pass
            messages = list_messages(page, token, chat_id)
            chat, image_map = parse_chat(meta, messages)
            n_imgs = _materialize_images(page, chat, image_map)
            stats = write_chat(chat, out_dir)
            if save_raw:
                try:
                    write_raw(stats['dir'], {'meta': meta, 'messages': messages})
                except Exception:
                    pass
            sync.mark(chat_id, updated, str(stats['md']))
            results.append({'id': chat_id, 'title': chat.title, 'ok': True,
                            'duration_s': round(time.time() - t0, 1),
                            **{k: stats[k] for k in ('turns', 'chars', 'images', 'docs')}})
            extra = f", {stats['images']} img" if stats['images'] else ''
            extra += f", {stats['docs']} doc" if stats['docs'] else ''
            print(f'[{i + 1}/{len(chats)}] {label} -> {stats["turns"]}t, '
                  f'{stats["chars"]:,} chars{extra}')
            if log:
                print(f'    images: {n_imgs} · {time.time() - t0:.1f}s')
        except Exception as e:
            print(f'[{i + 1}/{len(chats)}] {label} -> FAILED: {e}')
            traceback.print_exc()
            results.append({'id': chat_id, 'title': label, 'ok': False,
                            'error': str(e)})
        time.sleep(0.3)
    sync.save()
    return results


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog='reclaim kimi',
        description='Archive Kimi chats (update by default).')
    ap.add_argument('title', nargs='?',
                    help='fetch chats whose title contains this '
                         '(case-insensitive)')
    ap.add_argument('--rebuild', action='store_true',
                    help='fetch everything, overwriting local copies')
    ap.add_argument('--url', help='fetch one exact chat URL')
    ap.add_argument('--list', action='store_true',
                    help='print chat titles, no download')
    ap.add_argument('--log', action='store_true',
                    help='verbose per-chat progress and timings')
    ap.add_argument('--dry-run', action='store_true',
                    help='preview what would be fetched; nothing downloaded')
    ap.add_argument('--skip', type=int, default=0,
                    help='skip the first N chats in the listing')
    ap.add_argument('--limit', type=int, default=0,
                    help='fetch at most N chats')
    ap.add_argument('--no-raw', action='store_true',
                    help='do not save media-stripped raw.json per chat')
    ap.add_argument('-o', '--output-dir',
                    default=str(Path(__file__).resolve().parents[2]
                                / 'Kimi Chat'))
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
    token = ensure_logged_in(page)
    if args.url:
        cid = args.url.rstrip('/').split('/')[-1].split('?')[0]
        chats = [{'id': cid}]
    else:
        print('Listing chats...')
        chats = list_chats(page, token)
        print(f'Found {len(chats)} chats')
    if args.title:
        chats = [c for c in chats
                 if args.title.lower() in
                 ((c.get('name') or c.get('title') or '').lower())]
    if args.skip:
        chats = chats[args.skip:]
    if args.limit:
        chats = chats[:args.limit]
    if not chats:
        print('No chats matched.')
        return 1
    if args.list:
        for n, c in enumerate(chats, 1):
            print(f'{n}. {c.get("name") or c.get("title") or c["id"]}')
        print(f'-- {len(chats)} chat(s)')
        return 0
    skip_unchanged = not args.rebuild and not args.title
    if args.dry_run:
        sync = SyncState(out_dir, PROVIDER, migrate=False)
        updated_map = {c.get('id') or c.get('chat_id') or '':
                       c.get('updateTime') or c.get('updated_at')
                       for c in chats}
        return print_dry_run(chats, updated_map, sync, skip_unchanged)

    print(f"\n{'=' * 50}\n  {len(chats)} chats\n{'=' * 50}\n")
    results = run(page, token, chats, out_dir,
                  skip_unchanged=skip_unchanged,
                  save_raw=not args.no_raw, log=args.log)
    manifest = write_manifest(out_dir, PROVIDER, results, started)
    ok = sum(1 for r in results if r.get('ok'))
    print(f"\n{'=' * 50}")
    print(f'  Done: {ok} ok, {len(results) - ok} failed')
    print(f'  Manifest: {manifest}')
    print(f"{'=' * 50}")
    return 0 if ok == len(results) else 2


if __name__ == '__main__':
    raise SystemExit(main())
