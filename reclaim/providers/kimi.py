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
from ..core.manifest import SyncState, write_manifest
from ..core.model import Chat, Turn
from ..core.writer import write_chat

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
                          val: v.slice(0, 600)});
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
    page.goto(BASE_URL, wait_until='domcontentloaded', timeout=60000)
    time.sleep(4)
    try:
        return get_token(page)
    except RuntimeError:
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
        except RuntimeError:
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


def _msg_to_turn(msg: dict) -> Turn | None:
    role_raw = (msg.get('role') or msg.get('sender') or '').lower()
    role = 'user' if role_raw in ('user', 'human') else 'model'
    if role_raw == 'system':
        return None

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
                images.append({'url': v['url']})
            elif isinstance(v, str) and v.startswith('http'):
                images.append({'url': v})

    text = '\n'.join(t for t in texts if t and t.strip()).strip()
    if not text and not images:
        return None
    turn = Turn(role=role, text=text, thought=thought)
    if images:
        turn.attachments = []  # urls materialized later
        turn._kimi_image_urls = [i['url'] for i in images]  # noqa: SLF001
    return turn


def parse_chat(meta: dict, messages: list[dict]) -> Chat:
    chat_id = meta.get('id') or meta.get('chat_id') or ''
    title = meta.get('name') or meta.get('title') or ''
    turns = [t for t in (_msg_to_turn(m) for m in messages) if t]
    if not title and turns:
        first_user = next((t for t in turns if t.role == 'user'), None)
        title = ((first_user.text[:80].replace('\n', ' ') if first_user else '')
                 or '(untitled)')
    return Chat(id=chat_id, title=title,
                source_url=f'{BASE_URL}/chat/{chat_id}',
                turns=turns, provider=PROVIDER)


def _materialize_images(page, chat: Chat) -> int:
    ok = 0
    for turn in chat.turns:
        urls = getattr(turn, '_kimi_image_urls', None)
        if not urls:
            continue
        for u in urls:
            try:
                resp = page.request.get(u, timeout=60000)
                if resp.status == 200 and resp.body():
                    turn.images.append(resp.body())
                    ok += 1
            except Exception:
                pass
        del turn._kimi_image_urls  # noqa: SLF001
    return ok


def run(page, token: str, chats: list[dict], out_dir: Path,
        resume: bool = False) -> list[dict]:
    out_dir = Path(out_dir)
    sync = SyncState(out_dir, PROVIDER)
    results = []
    for i, item in enumerate(chats):
        chat_id = item.get('id') or item.get('chat_id') or ''
        label = (item.get('name') or item.get('title') or chat_id)[:55]
        updated = item.get('updateTime') or item.get('updated_at')
        if resume and sync.is_unchanged(chat_id, updated):
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
            chat = parse_chat(meta, messages)
            n_imgs = _materialize_images(page, chat)
            stats = write_chat(chat, out_dir)
            sync.mark(chat_id, updated, str(stats['md']))
            results.append({'id': chat_id, 'title': chat.title, 'ok': True,
                            'duration_s': round(time.time() - t0, 1),
                            **{k: stats[k] for k in ('turns', 'chars', 'images', 'docs')}})
            print(f'[{i + 1}/{len(chats)}] {label}')
            print(f"  -> {stats['turns']}t, {stats['chars']:,} chars"
                  + (f', {n_imgs} img' if n_imgs else ''))
        except Exception as e:
            print(f'[{i + 1}/{len(chats)}] {label} -> FAILED: {e}')
            traceback.print_exc()
            results.append({'id': chat_id, 'title': label, 'ok': False,
                            'error': str(e)})
        time.sleep(0.3)
    sync.save()
    return results


def main(argv=None):
    ap = argparse.ArgumentParser(prog='reclaim scrape kimi')
    ap.add_argument('--url', help='Scrape single chat URL')
    ap.add_argument('--only', help='Only chats whose title contains this substring')
    ap.add_argument('--start', type=int, default=0)
    ap.add_argument('--limit', type=int, default=0)
    ap.add_argument('--resume', action='store_true')
    ap.add_argument('-o', '--output-dir',
                    default=str(Path(__file__).resolve().parents[2] / 'Kimi Chat'))
    args = ap.parse_args(argv)
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    from playwright.sync_api import sync_playwright
    started = time.time()
    with sync_playwright() as p:
        ctx, page = browser.launch(p)
        try:
            token = ensure_logged_in(page)
            if args.url:
                cid = args.url.rstrip('/').split('/')[-1].split('?')[0]
                chats = [{'id': cid}]
            else:
                print('Listing chats...')
                chats = list_chats(page, token)
                print(f'Found {len(chats)} chats')
            if args.only:
                chats = [c for c in chats
                         if args.only.lower() in ((c.get('name') or c.get('title') or '').lower())]
            if args.start:
                chats = chats[args.start:]
            if args.limit:
                chats = chats[:args.limit]
            if not chats:
                print('No chats matched.')
                return 1

            print(f"\n{'=' * 50}\n  {len(chats)} chats\n{'=' * 50}\n")
            results = run(page, token, chats, out_dir, resume=args.resume)
            manifest = write_manifest(out_dir, PROVIDER, results, started)
            ok = sum(1 for r in results if r.get('ok'))
            print(f"\n{'=' * 50}")
            print(f'  Done: {ok} ok, {len(results) - ok} failed')
            print(f'  Manifest: {manifest}')
            print(f"{'=' * 50}")
            return 0 if ok == len(results) else 2
        finally:
            ctx.close()


if __name__ == '__main__':
    raise SystemExit(main())
