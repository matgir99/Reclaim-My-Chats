"""Claude provider (scrape mode) — claude.ai API replay.

Cookie auth only (no bearer token): the `sessionKey` cookie is the
session, `lastActiveOrg` names the org whose id prefixes every API path.
Page-side fetch() attaches the cookies automatically.

  GET /api/organizations/{orgId}/chat_conversations?offset=N&limit=M
  GET /api/organizations/{orgId}/chat_conversations/{uuid}
      ?tree=true&rendering_mode=messages&render_all_tools=true

Endpoints + shapes verified against live traffic by claude-exporter
(glebmish, docs/claude-ai-api.md; Kept commit ddfbcc2 confirms offset
pagination). Every chat carries abandoned edit-branches in
`chat_messages`; the active lineage is recovered by walking
`parent_message_uuid` pointers from `current_leaf_message_uuid` to the
root (fallback: full array, never render nothing). Thinking blocks are
removed without hiding the accompanying answer; tool_use/tool_result blocks are
skipped in v1 (known gap — artifacts/research reports live there).
"""

from __future__ import annotations

import json
import time
from types import SimpleNamespace
from urllib.parse import urlencode, urlsplit

from ..core import browser, engine
from ..core.events import EventSink
from ..core.model import Attachment, Chat, Turn

BASE_URL = 'https://claude.ai'
PROVIDER = 'claude'
LIST_LIMIT = 50

_GET_JS = """async (args) => {
    const resp = await fetch(args.url, {
        headers: {'accept': 'application/json'},
        credentials: 'include',
    });
    const text = await resp.text();
    return {status: resp.status, text};
}"""


def _get(page, url: str, retries: int = 3) -> dict:
    """Page-side GET (cookies attach automatically); returns parsed JSON."""
    last = None
    for attempt in range(retries):
        try:
            r = page.evaluate(_GET_JS, {'url': url})
            if r['status'] == 200:
                return json.loads(r['text'])
            if r['status'] == 404:
                raise RuntimeError('HTTP 404 — chat deleted?')
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


def _cookies(page) -> dict:
    return {c['name']: c['value'] for c in page.context.cookies()}


def ensure_logged_in(page) -> str:
    """Bring the session up, return the org id (uuid).

    Org id from the `lastActiveOrg` cookie; fallback: GET
    /api/organizations and take the first org's uuid. Waits for a human
    login (window shown) if the sessionKey cookie is missing.
    """
    # Page-side fetch() needs a claude.ai page even when the session cookie
    # already exists: from about:blank or another provider's page in
    # `reclaim all`, every request fails with "Failed to fetch".
    if urlsplit(page.url).hostname != urlsplit(BASE_URL).hostname:
        try:
            page.goto(BASE_URL, wait_until='domcontentloaded', timeout=60000)
        except Exception:
            pass
        time.sleep(4)
    if not _cookies(page).get('sessionKey'):
        page.bring_to_front()
        browser.set_window_bounds(page, state='normal', left=100, top=100)
        print('\n' + '=' * 50)
        print('  LOG IN to Claude in the browser window.')
        print('  Waiting (up to 15 minutes)...')
        print('=' * 50 + '\n')
        import os
        deadline = time.time() + int(os.environ.get('RECLAIM_LOGIN_TIMEOUT', '900'))
        while time.time() < deadline:
            if _cookies(page).get('sessionKey'):
                print('Logged in! Hiding window...\n')
                browser.set_window_bounds(page, state='minimized')
                time.sleep(2)
                break
            time.sleep(3)
        else:
            raise RuntimeError('Login timeout')
    org = _cookies(page).get('lastActiveOrg')
    if not org:
        data = _get(page, f'{BASE_URL}/api/organizations')
        orgs = data if isinstance(data, list) else data.get('organizations') or []
        if not orgs:
            raise RuntimeError('no organizations in /api/organizations — account issue?')
        org = orgs[0].get('uuid') or orgs[0].get('id')
        if not org:
            raise RuntimeError('unexpected organization shape: '
                               f'{json.dumps(orgs[0])[:200]}')
    return org


def list_chats(page, org: str) -> list[dict]:
    """GET .../chat_conversations, offset-paginated. Normalized items:
    {'id': uuid, 'title': name, 'updated_at': <ISO string>}."""
    items, offset, cursor = [], 0, None
    seen_ids, seen_cursors = set(), set()
    while True:
        query: dict[str, str | int] = {'limit': LIST_LIMIT}
        query['cursor' if cursor is not None else 'offset'] = cursor if cursor is not None else offset
        data = _get(page, f'{BASE_URL}/api/organizations/{org}'
                          f'/chat_conversations?{urlencode(query)}')
        cursor_mode = isinstance(data, dict) and any(
            key in data for key in ('conversations', 'items', 'cursor', 'nextCursor'))
        if isinstance(data, list):
            batch = data
        elif isinstance(data, dict):
            batch = next((data[key] for key in ('conversations', 'items', 'data')
                          if key in data), None)
        else:
            batch = None
        if not isinstance(batch, list):
            raise RuntimeError('unexpected Claude conversation list shape')
        added = 0
        for it in batch:
            if not isinstance(it, dict) or not it.get('uuid'):
                raise RuntimeError('unexpected Claude conversation row')
            if it['uuid'] in seen_ids:
                continue
            seen_ids.add(it['uuid'])
            items.append({'id': it['uuid'], 'title': it.get('name') or '',
                          'updated_at': it.get('updated_at')})
            added += 1
        if batch and not added:
            raise RuntimeError('Claude conversation pagination made no progress')
        if cursor_mode:
            cursor = data.get('nextCursor') or data.get('cursor')
            if not cursor:
                return items
            if not isinstance(cursor, str) or cursor in seen_cursors:
                raise RuntimeError('Claude returned an invalid or repeated conversation cursor')
            seen_cursors.add(cursor)
        elif not batch:
            return items
        offset += len(batch)


# ---------------------------------------------------------------------------
# Conversation parsing (pure, fixture-tested)
# ---------------------------------------------------------------------------
def active_lineage(messages: list[dict], leaf_uuid) -> list[dict]:
    """The current message chain: leaf -> parent pointers -> root, reversed.

    Every Claude chat keeps abandoned edit-branches in `chat_messages`;
    only this lineage is what the user sees. Fallback: full array when
    the leaf is missing (never render nothing).
    """
    by_uuid = {m.get('uuid'): m for m in messages if m.get('uuid')}
    if leaf_uuid and leaf_uuid in by_uuid:
        chain: list[dict] = []
        cur = leaf_uuid
        seen = set()
        while cur and cur in by_uuid and cur not in seen:
            seen.add(cur)
            m = by_uuid[cur]
            chain.append(m)
            parent = m.get('parent_message_uuid')
            cur = parent
        chain.reverse()
        return chain
    return list(messages)


def _block_turn(m: dict) -> tuple[str, bool, list[Attachment]]:
    """One message -> (text, had_thinking, attachments)."""
    sender = m.get('sender')
    if sender not in ('human', 'assistant'):
        return '', False, []
    texts, thought = [], False
    for block in m.get('content') or []:
        if not isinstance(block, dict):
            continue
        btype = block.get('type')
        if btype == 'text':
            t = block.get('text') or ''
            if t:
                texts.append(t)
        elif btype == 'thinking':
            thought = True
        # tool_use / tool_result / image blocks: skipped in v1 (documented gap)
    if not texts and isinstance(m.get('text'), str):
        texts.append(m['text'])
    atts: list[Attachment] = []
    for a in m.get('attachments') or []:  # pasted/uploaded files
        if not isinstance(a, dict):
            continue
        fname = a.get('file_name') or 'file'
        text = a.get('extracted_content') or ''
        atts.append(Attachment(filename=fname, kind='document',
                               data=text.encode('utf-8') if text else None,
                               description=fname,
                               source_url=a.get('file_url') or ''))
    for f in m.get('files') or []:  # image uploads (downloaded later)
        if not isinstance(f, dict):
            continue
        url = ((f.get('preview_asset') or {}).get('url')
               or f.get('preview_url') or '')
        fname = f.get('file_name') or 'image'
        atts.append(Attachment(filename=fname, kind='image', data=None,
                               source_url=url, description=fname))
    return '\n\n'.join(texts).strip(), thought, atts


def parse_chat(data: dict, chat_id: str, source_url: str,
               title: str = '') -> Chat:
    """Envelope JSON -> canonical Chat (pure; image bytes are None here)."""
    turns = []
    for m in active_lineage(data.get('chat_messages') or [],
                            data.get('current_leaf_message_uuid')):
        text, thought, atts = _block_turn(m)
        if not text and not atts and not thought:
            continue
        sender = m.get('sender')
        turns.append(Turn(role='user' if sender == 'human' else 'model',
                          text=text, thought=thought and not text and not atts,
                          had_thoughts=thought, attachments=atts))
    name = title or data.get('name') or '(untitled)'
    return Chat(id=chat_id, title=name, source_url=source_url,
                turns=turns, provider=PROVIDER)


def fetch_conversation(page, org: str, conv_id: str, title: str = '') -> tuple[Chat, dict]:
    data = _get(page, f'{BASE_URL}/api/organizations/{org}/chat_conversations/'
                      f'{conv_id}?tree=true&rendering_mode=messages'
                      f'&render_all_tools=true')
    chat = parse_chat(data, conv_id, f'{BASE_URL}/chat/{conv_id}', title)
    if not chat.visible_turns():
        raise RuntimeError('Claude returned no readable messages; response shape may have changed')
    # Materialize image uploads (signed preview URLs, page-side).
    for turn in chat.turns:
        for att in turn.attachments:
            if att.kind == 'image' and att.data is None and att.source_url:
                try:
                    resp = page.request.get(att.source_url, timeout=120000)
                    if resp.status == 200 and resp.body():
                        att.data = resp.body()
                except Exception:
                    pass
    return chat, data


class Claude(engine.Provider):
    """What only Claude does; the engine does the rest."""

    name = PROVIDER
    folder = 'Claude'
    description = 'Save Claude.ai conversations to the database (update by default).'
    noun = 'conversations'
    pace = 0.4

    def open(self, page):
        return SimpleNamespace(page=page, org=ensure_logged_in(page))

    def listing(self, s) -> list[engine.ChatRef]:
        return [engine.ChatRef(id=item.get('id', ''), title=item.get('title', ''),
                               updated_at=item.get('updated_at'), data=item)
                for item in list_chats(s.page, s.org)]

    def from_url(self, s, url: str) -> engine.ChatRef:
        return engine.ChatRef(id=url.rstrip('/').split('/')[-1].split('?')[0])

    def fetch(self, s, ref: engine.ChatRef) -> engine.Fetched:
        chat, raw = fetch_conversation(s.page, s.org, ref.id, ref.title)
        return engine.Fetched(chat, raw=raw)


SPEC = Claude()


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
