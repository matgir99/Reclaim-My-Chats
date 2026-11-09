"""Kimi provider (scrape mode) — native API replay.

Kimi serves accounts outside mainland China at kimi.ai (Google or non-+86
phone sign-in) and mainland accounts at kimi.com. Both run the same web app
and API. The default is kimi.ai; set RECLAIM_KIMI_URL=https://www.kimi.com
for a mainland account.

Endpoints and auth learned from Kept's MIT-licensed adapter
(docs/providers/kimi.md). The session Bearer token lives in the site's
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

import contextlib
import json
import os
import re
import time
from types import SimpleNamespace

from ..core import browser, engine, retry
from ..core.events import EventSink
from ..core.model import Chat, Turn

BASE_URL = os.environ.get('RECLAIM_KIMI_URL', '').strip().rstrip('/') or 'https://www.kimi.ai'
API = f'{BASE_URL}/apiv2'
PROVIDER = 'kimi'
PAGE_SIZE = 50
MSG_PAGE_SIZE = 1000
_THINK_HINT = re.compile(r'think|reason', re.IGNORECASE)

_FETCH_JS = """async (args) => {
    const resp = await fetch(args.url, {
        method: 'POST',
        headers: {'content-type': 'application/json', 'accept': 'application/json',
                  'authorization': 'Bearer ' + args.token},
        body: JSON.stringify(args.body),
        credentials: 'include',
    });
    const text = await resp.text();
    return {status: resp.status, text, retryAfter: resp.headers.get('retry-after')};
}"""


def _api(page, token: str, path: str, body: dict, retries: int = 3) -> dict:
    """Page-side POST with the shared retry policy (see core/retry.py)."""
    response = retry.send(lambda: page.evaluate(_FETCH_JS, {'url': f'{API}/{path}',
                                                            'token': token, 'body': body}),
                          what=f'Kimi {path}', retries=retries)
    return json.loads(response['text'])


def get_token(page) -> str:
    """Find the session token in Kimi's site storage and validate it."""
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
                                         cand['key'], re.IGNORECASE)):
            continue
        tried += 1
        try:
            _api(page, token, 'kimi.chat.v1.ChatService/ListChats',
                 {'project_id': '', 'page_size': 1, 'query': ''}, retries=1)
            return token
        except Exception:
            continue
    keys = ', '.join(f"{c['key']}[{c['len']}]" for c in inventory)
    raise RuntimeError(
        f'no valid Kimi token (tried {tried} candidates). '
        f'storage keys: {keys[:600]}')


def ensure_logged_in(page):
    with contextlib.suppress(Exception):  # navigation quirks are fine; the token probe decides
        page.goto(BASE_URL, wait_until='domcontentloaded', timeout=60000)
    time.sleep(4)
    try:
        return get_token(page)
    except Exception:
        pass
    # Kimi login is an SPA modal (no URL change): show the window and poll
    # until a valid token appears in localStorage.
    timeout = browser.login_timeout()
    browser.ask_for_login(page, PROVIDER, 'Kimi', timeout)
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            token = get_token(page)
            browser.login_done(page, PROVIDER)
            return token
        except Exception:
            time.sleep(3)
    raise RuntimeError('Login timeout')


def list_chats(page, token: str) -> list[dict]:
    """Every chat, paged with nextPageToken until a page has none. A
    repeated token is an error rather than a loop; a chat listed twice is
    kept once."""
    chats, page_token = [], ''
    seen_tokens, seen_ids = set(), set()
    while True:
        body = {'project_id': '', 'page_size': PAGE_SIZE, 'query': ''}
        if page_token:
            body['page_token'] = page_token
        data = _api(page, token, 'kimi.chat.v1.ChatService/ListChats', body)
        # Protobuf JSON omits empty fields: the page after the last chat is {}.
        batch = data.get('chats', [] if data == {} else None) if isinstance(data, dict) else None
        if not isinstance(batch, list):
            raise RuntimeError('unexpected Kimi conversation list shape')
        for item in batch:
            cid = item.get('id') or item.get('chat_id') if isinstance(item, dict) else None
            if not cid:
                raise RuntimeError('unexpected Kimi conversation row')
            if cid not in seen_ids:
                seen_ids.add(cid)
                chats.append(item)
        page_token = data.get('nextPageToken') or ''
        if not page_token:
            return chats
        if not isinstance(page_token, str) or page_token in seen_tokens:
            raise RuntimeError('Kimi returned an invalid or repeated conversation cursor')
        seen_tokens.add(page_token)


def list_messages(page, token: str, chat_id: str) -> list[dict]:
    data = _api(page, token, 'kimi.gateway.chat.v1.ChatService/ListMessages',
                {'chat_id': chat_id, 'page_size': MSG_PAGE_SIZE})
    msgs = next((data[key] for key in ('messages', 'items') if key in data), None)
    if not isinstance(msgs, list):
        raise RuntimeError('unexpected Kimi message list shape')
    if data.get('nextPageToken') or len(msgs) >= MSG_PAGE_SIZE:
        raise RuntimeError('Kimi message history may be truncated; refusing to save an incomplete chat')
    return list(reversed(msgs))  # API returns newest-first


def _block_text(block: dict) -> str:
    """A block's text: block.text (a string or {'content': ...}) or block.content."""
    text = block.get('text')
    text = (text.get('content') if isinstance(text, dict) else text) or block.get('content') or ''
    return text if isinstance(text, str) else ''


def _block_images(block: dict) -> list[str]:
    """Best-effort image URLs of a block, whatever key and shape it uses."""
    urls = []
    for key in ('image', 'image_url', 'img', 'url'):
        value = block.get(key)
        if isinstance(value, dict) and isinstance(value.get('url'), str):
            urls.append(value['url'])
        elif isinstance(value, str) and value.startswith('http'):
            urls.append(value)
    return urls


def _msg_to_turn(msg: dict) -> tuple[Turn | None, list[str]]:
    """Convert one API message. Returns (turn, [image urls])."""
    role_raw = (msg.get('role') or msg.get('sender') or '').lower()
    if role_raw not in ('user', 'human', 'assistant', 'model'):
        return None, []
    role = 'user' if role_raw in ('user', 'human') else 'model'
    texts, images, thought = [], [], False
    if isinstance(msg.get('content'), str):
        texts.append(msg['content'])
    elif isinstance(msg.get('text'), str):
        texts.append(msg['text'])
    for block in msg.get('blocks', []) or []:
        if not isinstance(block, dict):
            continue
        if _THINK_HINT.search(str(block.get('type') or '')):
            thought = True  # reasoning is not saved, only noted
            continue
        texts.append(_block_text(block))
        images += _block_images(block)
    text = '\n'.join(t for t in texts if t and t.strip()).strip()
    if not text and not images and not thought:
        return None, []
    return Turn(role=role, text=text, thought=thought and not text and not images,
                had_thoughts=thought), images


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


class Kimi(engine.Provider):
    """What only Kimi does; the engine does the rest."""

    name = PROVIDER
    folder = 'Kimi Chat'
    description = 'Save Kimi chats to the database (update by default).'
    pace = 0.3

    def open(self, page):
        return SimpleNamespace(page=page, token=ensure_logged_in(page))

    def listing(self, s) -> list[engine.ChatRef]:
        return [engine.ChatRef(id=c.get('id') or c.get('chat_id') or '',
                               title=c.get('name') or c.get('title') or '',
                               updated_at=c.get('updateTime') or c.get('updated_at'), data=c)
                for c in list_chats(s.page, s.token)]

    def from_url(self, s, url: str) -> engine.ChatRef:
        return engine.ChatRef(id=url.rstrip('/').split('/')[-1].split('?')[0])

    def fetch(self, s, ref: engine.ChatRef) -> engine.Fetched:
        meta = ref.data or {'id': ref.id}
        if not (meta.get('name') or meta.get('title')):
            try:  # a chat fetched by URL: its title comes from GetChat
                found = _api(s.page, s.token, 'kimi.chat.v1.ChatService/GetChat',
                             {'chat_id': ref.id}, retries=1)
                meta = found.get('chat') or found
            except Exception:
                pass
        messages = list_messages(s.page, s.token, ref.id)
        chat, image_map = parse_chat(meta, messages)
        if not chat.visible_turns():
            raise RuntimeError('Kimi returned no readable messages; response shape may have changed')
        images = _materialize_images(s.page, chat, image_map)
        return engine.Fetched(chat, raw={'meta': meta, 'messages': messages},
                              detail=f'images: {images}')


SPEC = Kimi()


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
