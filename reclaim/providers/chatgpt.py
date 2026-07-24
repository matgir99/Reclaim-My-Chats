"""ChatGPT provider (scrape mode) — native /backend-api/ replay.

Auth: session accessToken from /api/auth/session (in-page fetch with
cookies), then `Authorization: Bearer <token>` for /backend-api/ calls —
the same approach as pionxzh/chatgpt-exporter (MIT).

  GET /api/auth/session
  GET /backend-api/conversations?offset=0&limit=100&order=updated
  GET /backend-api/conversation/<id>
  GET /backend-api/files/<file_id>/download        (attachments/images)

Message trees are linearized with the same logic as the official-export
importer (root -> current_node). Asset pointers (file-service://file-…)
are downloaded with the session token — unlike the official export, this
path DOES recover images and files. Thoughts/hidden/system/tool messages
are excluded per project policy.
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
from ..core.model import Attachment, Chat, Turn
from ..core.writer import write_chat
from .chatgpt_import import _SKIP_CONTENT_TYPES, _IMG_EXT, _linearize

BASE_URL = 'https://chatgpt.com'
PROVIDER = 'chatgpt'
PAGE_LIMIT = 100

_GET_JS = """async (args) => {
    const resp = await fetch(args.url, {
        headers: {'authorization': 'Bearer ' + args.token, 'accept': 'application/json'},
        credentials: 'include',
    });
    const text = await resp.text();
    return {status: resp.status, text};
}"""


def _get(page, token: str, url: str, retries: int = 3) -> dict:
    last = None
    for attempt in range(retries):
        try:
            r = page.evaluate(_GET_JS, {'url': url, 'token': token})
            if r['status'] == 200:
                return json.loads(r['text'])
            if r['status'] in (401, 403):
                raise RuntimeError(f'auth failed ({r["status"]}) — session expired?')
            if r['status'] == 429:
                time.sleep(5 + attempt * 5)
                continue
            last = f'HTTP {r["status"]}: {r["text"][:120]}'
        except RuntimeError:
            raise
        except Exception as e:
            last = e
        time.sleep(2 + attempt * 2)
    raise RuntimeError(f'GET {url[:80]} failed: {last}')


def get_session_token(page) -> str:
    data = page.evaluate("""async () => {
        const r = await fetch('/api/auth/session', {credentials: 'include'});
        return {status: r.status, text: await r.text()};
    }""")
    if data['status'] != 200:
        raise RuntimeError(f'session fetch HTTP {data["status"]}')
    token = json.loads(data['text']).get('accessToken')
    if not token:
        raise RuntimeError('no accessToken in session — not logged in?')
    return token


def ensure_logged_in(page):
    page.goto(BASE_URL, wait_until='domcontentloaded', timeout=60000)
    time.sleep(4)
    try:
        return get_session_token(page)
    except RuntimeError:
        pass
    # Login may be an SPA flow (no reliable URL marker): show the window
    # and poll until a session token is available.
    page.bring_to_front()
    browser.set_window_bounds(page, state='normal', left=100, top=100)
    print('\n' + '=' * 50)
    print('  LOG IN to ChatGPT in the browser window.')
    print('  Waiting (up to 15 minutes)...')
    print('=' * 50 + '\n')
    import os
    deadline = time.time() + int(os.environ.get('RECLAIM_LOGIN_TIMEOUT', '900'))
    while time.time() < deadline:
        try:
            token = get_session_token(page)
            print('Logged in! Hiding window...\n')
            browser.set_window_bounds(page, state='minimized')
            time.sleep(2)
            return token
        except RuntimeError:
            time.sleep(3)
    raise RuntimeError('Login timeout')


def list_conversations(page, token: str) -> list[dict]:
    items, offset = [], 0
    while True:
        data = _get(page, token,
                    f'{BASE_URL}/backend-api/conversations?offset={offset}'
                    f'&limit={PAGE_LIMIT}&order=updated')
        batch = data.get('items') or []
        items.extend(batch)
        total = data.get('total', 0)
        offset += len(batch)
        if not batch or offset >= total:
            return items


# ---------------------------------------------------------------------------
# Conversation parsing (linearize + asset collection)
# ---------------------------------------------------------------------------
_ASSET_RE = re.compile(r'(?:file-service|sediment)://(file[-_][A-Za-z0-9]+)')


def _node_to_turn(node: dict) -> tuple[Turn | None, list[str]]:
    """Convert one mapping node. Returns (turn, [asset file ids])."""
    msg = node.get('message')
    if not msg:
        return None, []
    meta = msg.get('metadata') or {}
    if meta.get('is_visually_hidden_from_conversation'):
        return None, []
    role = (msg.get('author') or {}).get('role', '')
    if role not in ('user', 'assistant'):
        return None, []
    content = msg.get('content') or {}
    ctype = content.get('content_type', '')
    if ctype in _SKIP_CONTENT_TYPES:
        return None, []

    texts, asset_ids = [], []
    for part in content.get('parts', []) or []:
        if isinstance(part, str):
            texts.append(part)
        elif isinstance(part, dict):
            pointer = part.get('asset_pointer') or ''
            m = _ASSET_RE.search(pointer)
            if m:
                asset_ids.append(m.group(1).replace('_', '-', 1))
            elif part.get('text'):
                texts.append(part['text'])

    text = '\n\n'.join(t for t in texts if t and t.strip())
    if not text.strip() and not asset_ids:
        return None, []
    turn = Turn(role='user' if role == 'user' else 'model', text=text.strip())
    return turn, asset_ids


def _download_asset(page, token: str, file_id: str) -> tuple[bytes | None, str]:
    """Download a file-service asset. Returns (bytes, content_type)."""
    for url in (f'{BASE_URL}/backend-api/files/{file_id}/download',
                f'{BASE_URL}/backend-api/files/{file_id}'):
        try:
            resp = page.request.get(
                url, headers={'Authorization': f'Bearer {token}'}, timeout=120000)
            if resp.status == 200 and resp.body():
                return resp.body(), (resp.headers.get('content-type') or '')
        except Exception:
            continue
    return None, ''


def fetch_conversation(page, token: str, conv_id: str, title: str = '') -> Chat:
    data = _get(page, token, f'{BASE_URL}/backend-api/conversation/{conv_id}')
    nodes = _linearize(data.get('mapping') or {}, data.get('current_node'))
    turns = []
    for node in nodes:
        turn, asset_ids = _node_to_turn(node)
        if turn is None:
            continue
        for fid in asset_ids:
            blob, ctype = _download_asset(page, token, fid)
            if blob is None:
                turn.text += '\n\n*[attachment could not be downloaded]*'
                continue
            if ctype.startswith('image/'):
                turn.images.append(blob)
            else:
                name = f'file_{fid[:12]}'
                ext = {'application/pdf': '.pdf', 'text/csv': '.csv',
                       'text/plain': '.txt'}.get(ctype.split(';')[0], '.bin')
                turn.attachments.append(Attachment(
                    filename=name + ext, kind='document', data=blob,
                    description=fid))
        turns.append(turn)
    return Chat(id=conv_id, title=title or data.get('title', '') or '(untitled)',
                source_url=f'{BASE_URL}/c/{conv_id}', turns=turns,
                provider=PROVIDER)


def run(page, token: str, items: list[dict], out_dir: Path,
        resume: bool = False) -> list[dict]:
    out_dir = Path(out_dir)
    sync = SyncState(out_dir, PROVIDER)
    results = []
    for i, item in enumerate(items):
        cid = item.get('id', '')
        label = (item.get('title') or cid)[:55]
        updated = item.get('update_time')
        if resume and sync.is_unchanged(cid, updated):
            print(f'[{i + 1}/{len(items)}] {label} -> skip (unchanged)')
            continue
        t0 = time.time()
        try:
            chat = fetch_conversation(page, token, cid, item.get('title', ''))
            stats = write_chat(chat, out_dir)
            sync.mark(cid, updated, str(stats['md']))
            results.append({'id': cid, 'title': chat.title, 'ok': True,
                            'duration_s': round(time.time() - t0, 1),
                            **{k: stats[k] for k in ('turns', 'chars', 'images', 'docs')}})
            extra = f", {stats['images']} img" if stats['images'] else ''
            extra += f", {stats['docs']} doc" if stats['docs'] else ''
            print(f'[{i + 1}/{len(items)}] {label}')
            print(f"  -> {stats['turns']}t, {stats['chars']:,} chars{extra}")
        except Exception as e:
            print(f'[{i + 1}/{len(items)}] {label} -> FAILED: {e}')
            traceback.print_exc()
            results.append({'id': cid, 'title': label, 'ok': False, 'error': str(e)})
        time.sleep(0.4)
    sync.save()
    return results


def main(argv=None):
    ap = argparse.ArgumentParser(prog='reclaim scrape chatgpt')
    ap.add_argument('--url', help='Scrape single chat URL')
    ap.add_argument('--only', help='Only chats whose title contains this substring')
    ap.add_argument('--start', type=int, default=0)
    ap.add_argument('--limit', type=int, default=0)
    ap.add_argument('--resume', action='store_true')
    ap.add_argument('-o', '--output-dir',
                    default=str(Path(__file__).resolve().parents[2] / 'ChatGPT'))
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
                items = [{'id': cid, 'title': ''}]
            else:
                print('Listing conversations...')
                items = list_conversations(page, token)
                print(f'Found {len(items)} conversations')
            if args.only:
                items = [c for c in items
                         if args.only.lower() in (c.get('title') or '').lower()]
            if args.start:
                items = items[args.start:]
            if args.limit:
                items = items[:args.limit]
            if not items:
                print('No conversations matched.')
                return 1

            print(f"\n{'=' * 50}\n  {len(items)} conversations\n{'=' * 50}\n")
            results = run(page, token, items, out_dir, resume=args.resume)
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
