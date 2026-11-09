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

import json
import re
import time
from types import SimpleNamespace
from urllib.parse import urlencode

from ..core import browser, engine, retry
from ..core.events import EventSink
from ..core.model import Attachment, Chat, Turn
from .chatgpt_import import (_SKIP_CONTENT_TYPES, _IMG_EXT, _content_parts, _is_tool_call,
                             _linearize)

BASE_URL = 'https://chatgpt.com'
PROVIDER = 'chatgpt'
PAGE_LIMIT = 100

_GET_JS = """async (args) => {
    const headers = {'authorization': 'Bearer ' + args.token,
                     'x-authorization': 'Bearer ' + args.token,
                     'accept': 'application/json'};
    const selected = document.cookie.split(';').map(s => s.trim())
        .find(s => s.startsWith('_account='));
    if (selected) {
        const workspace = decodeURIComponent(selected.slice('_account='.length));
        if (!window.__reclaimAccount || window.__reclaimAccount.workspace !== workspace) {
            const check = await fetch('/backend-api/accounts/check/v4-2023-04-27', {
                headers, credentials: 'include', signal: AbortSignal.timeout(60000),
            });
            if (!check.ok) return {status: check.status, text: 'workspace lookup failed'};
            const accounts = (await check.json()).accounts || {};
            // The cookie can be an accounts key, an account id, or "personal"
            // while accounts are keyed by id and "default". Require one match.
            const ids = new Set(Object.entries(accounts).filter(([key, entry]) =>
                key === workspace || entry?.account?.account_id === workspace
                || (workspace === 'personal' && entry?.account?.structure === 'personal'))
                .map(([, entry]) => entry?.account?.account_id).filter(Boolean));
            const accountId = ids.size === 1 ? [...ids][0] : null;
            if (!accountId) throw new Error('Selected ChatGPT workspace could not be resolved; reload ChatGPT and retry');
            window.__reclaimAccount = {workspace, accountId};
        }
        headers['Chatgpt-Account-Id'] = window.__reclaimAccount.accountId;
    }
    const resp = await fetch(args.url, {
        headers,
        credentials: 'include',
        signal: AbortSignal.timeout(60000),
    });
    const text = await resp.text();
    return {status: resp.status, text, retryAfter: resp.headers.get('retry-after')};
}"""

# Kept for tests and callers that read the rate-limit schedule here.
RATE_LIMIT_WAITS = retry.RATE_LIMIT_WAITS


def _get(page, token: str, url: str, retries: int = 3) -> dict:
    """Page-side GET with the shared retry policy (see core/retry.py)."""
    response = retry.send(lambda: page.evaluate(_GET_JS, {'url': url, 'token': token}),
                          what=f'ChatGPT GET {url[:80]}', retries=retries)
    return json.loads(response['text'])


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
    except Exception as exc:
        raise RuntimeError(
            f'session not JSON (head: {data["text"][:80]!r}) — not logged in?') from exc
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
    seen = set()
    while True:
        data = _get(page, token,
                    f'{BASE_URL}/backend-api/conversations?offset={offset}'
                    f'&limit={PAGE_LIMIT}&order=updated')
        batch = data.get('items') if isinstance(data, dict) else None
        if not isinstance(batch, list):
            raise RuntimeError('unexpected ChatGPT conversation list shape')
        added = 0
        for item in batch:
            if not isinstance(item, dict) or not item.get('id'):
                raise RuntimeError('unexpected ChatGPT conversation row')
            if item['id'] not in seen:
                seen.add(item['id'])
                items.append(item)
                added += 1
        offset += len(batch)
        # The service can report a null or underestimated total. Only an
        # empty page proves this listing is exhausted, even after short pages.
        if not batch:
            return items
        if not added:
            raise RuntimeError('ChatGPT conversation pagination made no progress')


# ---------------------------------------------------------------------------
# Projects (OpenAI calls them "gizmos" internally; flow per MIT-licensed
# pionxzh/chatgpt-exporter). Chats inside Projects are INVISIBLE to the main
# /conversations listing - they must be enumerated per project.
# ---------------------------------------------------------------------------
def list_projects(page, token: str) -> list[dict]:
    """GET /backend-api/gizmos/snorlax/sidebar (cursor-paginated).
    Returns [{'id': 'g-p-...', 'name': '...'}]."""
    projects, cursor = [], None
    seen_cursors, seen_ids = set(), set()
    while True:
        url = (f'{BASE_URL}/backend-api/gizmos/snorlax/sidebar'
               f'?conversations_per_gizmo=0')
        if cursor is not None:
            url += '&' + urlencode({'cursor': cursor})
        data = _get(page, token, url)
        batch = data.get('items') if isinstance(data, dict) else None
        if not isinstance(batch, list):
            raise RuntimeError('unexpected ChatGPT project list shape')
        for it in batch:
            g = (it.get('gizmo') or {}).get('gizmo') or {}
            gid = g.get('id')
            if gid and gid not in seen_ids:
                seen_ids.add(gid)
                name = (g.get('display') or {}).get('name') or g.get('name') or ''
                projects.append({'id': gid, 'name': name})
        cursor = data.get('cursor')
        if cursor is None:
            return projects
        if cursor in seen_cursors:
            raise RuntimeError('ChatGPT returned a repeated project cursor')
        seen_cursors.add(cursor)


def list_project_conversations(page, token: str, gizmo_id: str) -> list[dict]:
    """GET /backend-api/gizmos/<id>/conversations (alphanumeric cursor)."""
    items, cursor = [], 0
    seen_cursors, seen_ids = set(), set()
    while True:
        data = _get(page, token,
                    f'{BASE_URL}/backend-api/gizmos/{gizmo_id}/conversations'
                    f'?{urlencode({"cursor": cursor, "limit": 50})}')
        batch = data.get('items') if isinstance(data, dict) else None
        if not isinstance(batch, list):
            raise RuntimeError('unexpected ChatGPT project conversation list shape')
        for item in batch:
            if not isinstance(item, dict) or not item.get('id'):
                raise RuntimeError('unexpected ChatGPT project conversation row')
            if item['id'] not in seen_ids:
                seen_ids.add(item['id'])
                items.append(item)
        cursor = data.get('cursor')
        if not batch or cursor is None:
            return items
        if cursor in seen_cursors:
            raise RuntimeError('ChatGPT returned a repeated project conversation cursor')
        seen_cursors.add(cursor)


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
            raise RuntimeError(
                f"Could not list ChatGPT project '{p['name']}': {e}. "
                'The database may be incomplete; retry the update.'
            ) from e
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
    if meta.get('is_visually_hidden_from_conversation') or meta.get('is_thinking_preamble_message'):
        return None, []
    if _is_tool_call(msg):
        return None, []
    role = (msg.get('author') or {}).get('role', '')
    if role not in ('user', 'assistant'):
        return None, []
    content = msg.get('content') or {}
    ctype = content.get('content_type', '')
    if ctype in _SKIP_CONTENT_TYPES:
        return None, []

    texts, asset_ids = [], []
    for part in _content_parts(msg):
        if isinstance(part, str):
            texts.append(part)
        elif isinstance(part, dict):
            pointer = part.get('asset_pointer') or ''
            m = _ASSET_RE.search(pointer)
            if m:
                asset_ids.append(m.group(1))
            elif part.get('text'):
                texts.append(part['text'])

    for attachment in meta.get('attachments') or []:
        if isinstance(attachment, dict):
            fid = attachment.get('id')
            if isinstance(fid, str) and re.fullmatch(r'file[-_][A-Za-z0-9-]+', fid):
                asset_ids.append(fid)
    asset_ids = list(dict.fromkeys(asset_ids))
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
        meta = _get(page, token, meta_url)
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
        uploads = {a['id']: a for a in (((node.get('message') or {}).get('metadata') or {}).get('attachments') or [])
                   if isinstance(a, dict) and a.get('id')}
        for fid in asset_ids:
            blob, meta = _download_asset(page, token, fid)
            if blob is None:
                turn.text += '\n\n*[attachment could not be downloaded]*'
                continue
            mime = (meta.get('mime_type') or '').split(';')[0]
            name = meta.get('file_name') or uploads.get(fid, {}).get('name') or f'file_{fid[:12]}'
            if mime.startswith('image/'):
                turn.images.append(blob)
            else:
                if '.' not in name:
                    name += {'application/pdf': '.pdf'}.get(mime, '')
                turn.attachments.append(Attachment(
                    filename=name,
                    kind='image' if _IMG_EXT.search(name) else 'document',
                    data=blob, description=name))
        turns.append(turn)
    chat = Chat(id=conv_id, title=title or data.get('title', '') or '(untitled)',
                source_url=f'{BASE_URL}/c/{conv_id}', turns=turns, provider=PROVIDER)
    if not chat.visible_turns():
        raise RuntimeError('ChatGPT returned no readable messages; response shape may have changed')
    return chat, data


def _project_name(item: dict, data: dict, project_names: dict[str, str] | None) -> str | None:
    """The listing's project, else the project named by the conversation itself.

    A chat fetched by URL has no listing entry; without this it would be
    saved outside its project folder."""
    if '_project' in item or not project_names:
        return item.get('_project')
    if data.get('gizmo_type') != 'snorlax':
        return None
    return project_names.get(data.get('gizmo_id') or '')


class ChatGPT(engine.Provider):
    """What only ChatGPT does; the engine does the rest."""

    name = PROVIDER
    folder = 'ChatGPT'
    description = ('Save ChatGPT conversations incl. Projects to the database '
                   '(update by default).')
    noun = 'conversations'
    pace = 0.4

    def open(self, page):
        return SimpleNamespace(page=page, token=ensure_logged_in(page), project_names=None)

    def announce_listing(self) -> None:
        print('Listing conversations (main list + projects)...')

    def announce_found(self, refs) -> None:
        in_projects = sum(1 for r in refs if (r.data or {}).get('_project'))
        print(f'Found {len(refs)} conversations ({in_projects} in projects)')

    def listing(self, s) -> list[engine.ChatRef]:
        return [engine.ChatRef(id=item.get('id', ''), title=item.get('title') or '',
                               updated_at=item.get('update_time'), data=item)
                for item in list_all_conversations(s.page, s.token)]

    def from_url(self, s, url: str) -> engine.ChatRef:
        return engine.ChatRef(id=url.rstrip('/').split('/')[-1].split('?')[0], data={})

    def fetch(self, s, ref: engine.ChatRef) -> engine.Fetched:
        item = ref.data or {}
        chat, raw = fetch_conversation(s.page, s.token, ref.id, item.get('title') or ref.title)
        if '_project' not in item and s.project_names is None:
            # Fetched by URL: find its project from the conversation itself.
            s.project_names = {p['id']: p['name'] for p in list_projects(s.page, s.token)}
        chat.project = _project_name(item, raw, s.project_names)
        return engine.Fetched(chat, raw=raw)


SPEC = ChatGPT()


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
