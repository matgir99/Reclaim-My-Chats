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

from ..core import browser, config
from ..core.manifest import SyncState, print_dry_run, write_manifest
from ..core.model import Attachment, Chat, Turn
from ..core.progress import progress
from ..core.writer import write_chat, write_raw
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
        try {
            const r = await fetch('/api/auth/session', {credentials: 'include'});
            return {status: r.status, text: await r.text()};
        } catch (e) { return {status: 0, text: String(e)}; }
    }""")
    if data['status'] != 200:
        raise RuntimeError(f'session fetch HTTP {data["status"]}')
    try:
        token = json.loads(data['text']).get('accessToken')
    except Exception:
        raise RuntimeError(
            f'session not JSON (head: {data["text"][:80]!r}) — not logged in?')
    if not token:
        raise RuntimeError('no accessToken in session — not logged in?')
    return token


def ensure_logged_in(page):
    try:
        page.goto(BASE_URL, wait_until='domcontentloaded', timeout=60000)
    except Exception:
        pass  # navigation quirks are fine; token probe decides
    time.sleep(4)
    try:
        return get_session_token(page)
    except Exception:
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
        except Exception:
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
# Projects (OpenAI calls them "gizmos" internally; flow per MIT-licensed
# pionxzh/chatgpt-exporter). Chats inside Projects are INVISIBLE to the main
# /conversations listing - they must be enumerated per project.
# ---------------------------------------------------------------------------
def list_projects(page, token: str) -> list[dict]:
    """GET /backend-api/gizmos/snorlax/sidebar (cursor-paginated).
    Returns [{'id': 'g-p-...', 'name': '...'}]."""
    projects, cursor = [], None
    while True:
        url = (f'{BASE_URL}/backend-api/gizmos/snorlax/sidebar'
               f'?conversations_per_gizmo=0')
        if cursor is not None:
            url += f'&cursor={cursor}'
        data = _get(page, token, url)
        for it in data.get('items') or []:
            g = (it.get('gizmo') or {}).get('gizmo') or {}
            gid = g.get('id')
            if gid:
                name = (g.get('display') or {}).get('name') or g.get('name') or ''
                projects.append({'id': gid, 'name': name})
        cursor = data.get('cursor')
        if cursor is None:
            return projects


def list_project_conversations(page, token: str, gizmo_id: str) -> list[dict]:
    """GET /backend-api/gizmos/<id>/conversations (alphanumeric cursor)."""
    items, cursor = [], 0
    while True:
        data = _get(page, token,
                    f'{BASE_URL}/backend-api/gizmos/{gizmo_id}/conversations'
                    f'?cursor={cursor}&limit=50')
        batch = data.get('items') or []
        items.extend(batch)
        cursor = data.get('cursor')
        if not batch or cursor is None:
            return items


def list_all_conversations(page, token: str) -> list[dict]:
    """Projects FIRST, then the main list; deduped by chat id.
    Each item gets a '_project' field (None for unfiled chats).

    Projects must be added first: the main /conversations list can include
    project chats in its items (its 'total' field is unreliable), so project
    tagging must happen before the main list dedups them away."""
    seen: set[str] = set()
    out: list[dict] = []

    def add(items, project):
        for it in items:
            cid = it.get('id')
            if cid and cid not in seen:
                seen.add(cid)
                it['_project'] = project
                out.append(it)

    for p in list_projects(page, token):
        try:
            add(list_project_conversations(page, token, p['id']), p['name'])
        except Exception as e:
            print(f"  WARNING: listing project '{p['name']}' failed: {e}")
    add(list_conversations(page, token), None)
    return out


# ---------------------------------------------------------------------------
# Conversation parsing (linearize + asset collection)
# ---------------------------------------------------------------------------
_ASSET_RE = re.compile(r'(?:file-service|sediment)://(file[-_][A-Za-z0-9-]+)')


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
                asset_ids.append(m.group(1))
            elif part.get('text'):
                texts.append(part['text'])

    text = '\n\n'.join(t for t in texts if t and t.strip())
    if not text.strip() and not asset_ids:
        return None, []
    turn = Turn(role='user' if role == 'user' else 'model', text=text.strip())
    return turn, asset_ids


def _download_asset(page, token: str, file_id: str) -> tuple[bytes | None, dict]:
    """Two-step file download (per pionxzh/chatgpt-exporter, MIT):
    1. GET /backend-api/files/download/<id> -> JSON {status, download_url,
       file_name, mime_type, file_size_bytes}
    2. GET the signed download_url -> bytes
    Returns (bytes, meta)."""
    meta_url = f'{BASE_URL}/backend-api/files/download/{file_id}?post_id=&inline=false'
    try:
        resp = page.request.get(
            meta_url, headers={'Authorization': f'Bearer {token}'},
            timeout=120000)
        if resp.status != 200:
            return None, {}
        meta = resp.json()
    except Exception:
        return None, {}
    if meta.get('status') != 'success' or not meta.get('download_url'):
        return None, meta
    try:
        blob = page.request.get(meta['download_url'], timeout=120000)
        if blob.status == 200 and blob.body():
            return blob.body(), meta
    except Exception:
        pass
    return None, meta


def fetch_conversation(page, token: str, conv_id: str, title: str = '') -> tuple[Chat, dict]:
    data = _get(page, token, f'{BASE_URL}/backend-api/conversation/{conv_id}')
    nodes = _linearize(data.get('mapping') or {}, data.get('current_node'))
    turns = []
    for node in nodes:
        turn, asset_ids = _node_to_turn(node)
        if turn is None:
            continue
        for fid in asset_ids:
            blob, meta = _download_asset(page, token, fid)
            if blob is None:
                turn.text += '\n\n*[attachment could not be downloaded]*'
                continue
            mime = (meta.get('mime_type') or '').split(';')[0]
            name = meta.get('file_name') or f'file_{fid[:12]}'
            if mime.startswith('image/'):
                turn.images.append(blob)
            else:
                if '.' not in name:
                    name += {'.pdf': 'application/pdf'}.get(mime, '') or ''
                turn.attachments.append(Attachment(
                    filename=name,
                    kind='image' if _IMG_EXT.search(name) else 'document',
                    data=blob, description=name))
        turns.append(turn)
    return (Chat(id=conv_id, title=title or data.get('title', '') or '(untitled)',
                 source_url=f'{BASE_URL}/c/{conv_id}', turns=turns,
                 provider=PROVIDER), data)


def run(page, token: str, items: list[dict], out_dir: Path,
        skip_unchanged: bool = False, save_raw: bool = True,
        log: bool = False) -> list[dict]:
    """Fetch the given conversations. Returns per-chat results for the manifest.

    Per-chat lines print only with log=True (default output is
    summary-level; failures always print)."""
    out_dir = Path(out_dir)
    sync = SyncState(out_dir, PROVIDER)
    results = []
    t_run = time.time()
    for i, item in enumerate(items):
        cid = item.get('id', '')
        label = (item.get('title') or cid)[:55]
        updated = item.get('update_time')
        if log:
            print(progress(i + 1, len(items), t_run))
        if skip_unchanged and sync.is_unchanged(cid, updated):
            if log:
                print(f'[{i + 1}/{len(items)}] {label} -> skip (unchanged)')
            continue
        t0 = time.time()
        try:
            chat, raw = fetch_conversation(page, token, cid, item.get('title', ''))
            chat.project = item.get('_project')
            stats = write_chat(chat, out_dir)
            if save_raw:
                try:
                    write_raw(stats['dir'], raw)
                except Exception:
                    pass
            sync.mark(cid, updated, str(stats['md']))
            results.append({'id': cid, 'title': chat.title, 'ok': True,
                            'duration_s': round(time.time() - t0, 1),
                            **{k: stats[k] for k in ('turns', 'chars', 'images', 'docs')}})
            if log:
                extra = f", {stats['images']} img" if stats['images'] else ''
                extra += f", {stats['docs']} doc" if stats['docs'] else ''
                print(f'[{i + 1}/{len(items)}] {label} -> {stats["turns"]}t, '
                      f'{stats["chars"]:,} chars{extra}')
                print(f'    assets: {stats["images"] + stats["docs"]} · '
                      f'{time.time() - t0:.1f}s')
        except Exception as e:
            print(f'[{i + 1}/{len(items)}] {label} -> FAILED: {e}')
            traceback.print_exc()
            results.append({'id': cid, 'title': label, 'ok': False, 'error': str(e)})
        time.sleep(0.4)
    sync.save()
    return results


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog='reclaim chatgpt',
        description='Archive ChatGPT conversations incl. Projects '
                    '(update by default).')
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
                    default=str(config.archive_root() / 'ChatGPT'))
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
        items = [{'id': cid, 'title': ''}]
    else:
        print('Listing conversations (main list + projects)...')
        items = list_all_conversations(page, token)
        n_proj = sum(1 for c in items if c.get('_project'))
        print(f'Found {len(items)} conversations ({n_proj} in projects)')
    if args.title:
        items = [c for c in items
                 if args.title.lower() in (c.get('title') or '').lower()]
    if args.skip:
        items = items[args.skip:]
    if args.limit:
        items = items[:args.limit]
    if not items:
        print('No chats matched.')
        return 1
    if args.list:
        for n, c in enumerate(items, 1):
            print(f'{n}. {c.get("title") or c["id"]}')
        print(f'-- {len(items)} chat(s)')
        return 0
    skip_unchanged = not args.rebuild and not args.title
    if args.dry_run:
        sync = SyncState(out_dir, PROVIDER, migrate=False)
        updated_map = {c.get('id', ''): c.get('update_time')
                       for c in items}
        return print_dry_run(items, updated_map, sync, skip_unchanged,
                             log=args.log)

    print(f"\n{'=' * 50}\n  {len(items)} conversations\n{'=' * 50}\n")
    results = run(page, token, items, out_dir,
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
