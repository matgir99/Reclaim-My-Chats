"""DeepSeek Chat provider — authenticated history API plus cached discovery.

List both pinned and unpinned server history; supplement it with IDs from
IndexedDB, which only contains chats previously loaded in this browser.
Fetch selected conversations from the history API. The service may limit
older list results; neither an empty cache nor a short API list proves the
account's entire history has been recovered (see docs/UPSTREAM_REVIEW.md).

Fragment handling: REQUEST (user) and RESPONSE (assistant) are kept; THINK
(model reasoning) is dropped; SEARCH fragments provide citation URLs mapped
from [citation:N] to [-N](url).
"""

from __future__ import annotations

import json
import os
import re
import time
from types import SimpleNamespace
from urllib.parse import urlencode

from ..core import browser, engine, retry
from ..core.events import EventSink
from ..core.model import Attachment, Chat, Turn

BASE_URL = 'https://chat.deepseek.com/'
PROVIDER = 'deepseek'
_IMG_EXTS = re.compile(r'\.(png|jpe?g|gif|webp)$', re.I)


# ---------------------------------------------------------------------------
# Login + API reads (tokens stay inside the browser page)
# ---------------------------------------------------------------------------
_HAS_TOKEN_JS = """() => {
    try { return !!JSON.parse(localStorage.getItem('userToken') || '{}').value; }
    catch { return false; }
}"""

_GET_JS = """async (args) => {
    let token;
    try { token = JSON.parse(localStorage.getItem('userToken') || '{}').value; }
    catch { /* handled below */ }
    if (typeof token !== 'string' || !token) throw new Error('DeepSeek login required');
    const response = await fetch(args.url, {
        credentials: 'include',
        headers: {'accept': 'application/json', 'authorization': 'Bearer ' + token},
        signal: AbortSignal.timeout(60000),
    });
    return {status: response.status, text: await response.text(),
            retryAfter: response.headers.get('retry-after')};
}"""


def ensure_logged_in(page):
    page.goto(BASE_URL, wait_until='domcontentloaded', timeout=60000)
    time.sleep(3)
    if page.evaluate(_HAS_TOKEN_JS):
        return
    page.bring_to_front()
    browser.set_window_bounds(page, state='normal', left=100, top=100)
    print('\nLog in to DeepSeek in the browser window. Waiting up to 15 minutes...')
    deadline = time.time() + int(os.environ.get('RECLAIM_LOGIN_TIMEOUT', '900'))
    while time.time() < deadline:
        if page.evaluate(_HAS_TOKEN_JS):
            browser.set_window_bounds(page, state='minimized')
            return
        time.sleep(3)
    raise RuntimeError('DeepSeek login timeout')


def _api_get(page, path: str, params: dict | None = None) -> dict:
    url = BASE_URL + 'api/v0/' + path
    if params:
        url += '?' + urlencode(params)
    response = retry.send(lambda: page.evaluate(_GET_JS, {'url': url}),
                          what='DeepSeek history API')
    try:
        envelope = json.loads(response['text'])
        data = envelope['data']
        if envelope.get('code') != 0 or data.get('biz_code') != 0:
            raise RuntimeError('DeepSeek history API rejected the request; check your login and retry')
        result = data['biz_data']
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise RuntimeError('unexpected DeepSeek history API response') from exc
    if not isinstance(result, dict):
        raise RuntimeError('unexpected DeepSeek history API payload')
    return result


def list_chats(page) -> list[dict]:
    """One pass over the server listing, pinned chats first.

    The cursor is the last row's position, (pinned, updated_at), in a list
    sorted pinned-first. It must use that row's own pinned flag: asking for
    pinned=true before an unpinned row's time restarts the unpinned section,
    so a separate pinned pass repeats the first page forever. A page that
    adds no chat while more are reported is an error, not a complete list.
    """
    chats, seen, seen_cursors = [], set(), set()
    params: dict[str, str | int | float] = {'lte_cursor.pinned': 'true'}
    while True:
        data = _api_get(page, 'chat_session/fetch_page', params)
        batch = data.get('chat_sessions')
        if not isinstance(batch, list) or not isinstance(data.get('has_more'), bool):
            raise RuntimeError('unexpected DeepSeek conversation list shape')
        added = 0
        for item in batch:
            if not isinstance(item, dict) or not item.get('id'):
                raise RuntimeError('unexpected DeepSeek conversation row')
            if item['id'] not in seen:
                seen.add(item['id'])
                chats.append(item)
                added += 1
        if not data['has_more']:
            return chats
        last = batch[-1] if batch else {}
        cursor = (bool(last.get('pinned')), last.get('updated_at'))
        if (not added or not isinstance(cursor[1], (int, float))
                or isinstance(cursor[1], bool) or cursor in seen_cursors):
            raise RuntimeError('DeepSeek conversation pagination made no progress; the database may be incomplete')
        seen_cursors.add(cursor)
        params = {'lte_cursor.pinned': 'true' if cursor[0] else 'false',
                  'lte_cursor.updated_at': cursor[1]}


def fetch_record(page, chat_id: str) -> dict:
    data = _api_get(page, 'chat/history_messages',
                    {'chat_session_id': chat_id, 'cache_version': 0})
    if (not isinstance(data.get('chat_session'), dict)
            or data['chat_session'].get('id') != chat_id
            or not isinstance(data.get('chat_messages'), list)):
        raise RuntimeError('unexpected DeepSeek conversation shape')
    return {'data': data}


def read_summaries(page) -> list[dict]:
    """Optional discovery of previously cached chat IDs, without creating a DB."""
    return page.evaluate("""() => {
        return new Promise((resolve, reject) => {
            const timer = setTimeout(() => reject(new Error('Cache read timed out')), 10000);
            const done = (rows) => { clearTimeout(timer); resolve(rows); };
            const fail = () => { clearTimeout(timer); reject(new Error('Cache read failed')); };
            const req = indexedDB.open('deepseek-chat');
            req.onupgradeneeded = () => { req.transaction.abort(); done([]); };
            req.onblocked = fail;
            req.onerror = fail;
            req.onsuccess = (e) => {
                const db = e.target.result;
                if (!db.objectStoreNames.contains('history-message')) { db.close(); done([]); return; }
                let tx;
                try { tx = db.transaction('history-message', 'readonly'); }
                catch { db.close(); fail(); return; }
                tx.onabort = () => { db.close(); fail(); };
                const getAll = tx.objectStore('history-message').getAll();
                getAll.onsuccess = () => {
                    db.close();
                    done(getAll.result.map(rec => {
                        const s = rec.data?.chat_session || {};
                        return {id: s.id, title: s.title || '', updated_at: s.updated_at};
                    }).filter(s => s.id));
                };
                getAll.onerror = () => { db.close(); fail(); };
            };
        });
    }""")


# ---------------------------------------------------------------------------
# Record processing (API response or cached record -> canonical Chat)
# ---------------------------------------------------------------------------
def build_citation_map(fragments: list[dict]) -> dict:
    citemap = {}
    for frag in fragments:
        if frag.get('type') == 'SEARCH':
            for result in frag.get('results', []):
                idx = result.get('cite_index')
                if idx:
                    citemap[idx] = {'url': result.get('url', ''),
                                    'title': result.get('title', '')}
    return citemap


def replace_citations(text: str, citemap: dict) -> str:
    def replacer(match):
        n = int(match.group(1))
        url = citemap.get(n, {}).get('url', '')
        return f'[-{n}]({url})' if url else f'[-{n}]'
    return re.sub(r'\[citation:(\d+)\]', replacer, text)


def record_to_chat(record: dict) -> Chat:
    """Convert a history response or legacy cached record into a canonical Chat.

    FILE fragments (user uploads) become attachments whose ``source_url`` is
    the signed download path; bytes are fetched later by
    :func:`materialize_attachments` (needs the live page)."""
    data = record.get('data', record)
    data = data.get('biz_data', data)
    session = data.get('chat_session', {})
    messages = data.get('chat_messages', [])
    leaf = session.get('current_message_id')
    by_id = {m['message_id']: m for m in messages if m.get('message_id') is not None}
    if leaf is not None and leaf in by_id:
        chain, seen = [], set()
        while leaf is not None and leaf in by_id:
            if leaf in seen:
                raise RuntimeError('DeepSeek message tree contains a cycle')
            seen.add(leaf)
            message = by_id[leaf]
            chain.append(message)
            leaf = message.get('parent_id')
        messages = list(reversed(chain))

    all_fragments = []
    for msg in messages:
        all_fragments.extend(msg.get('fragments') or [])
    citemap = build_citation_map(all_fragments)

    turns = []
    for msg in messages:
        role = msg.get('role', '').upper()
        fragments = msg.get('fragments') or []
        if role == 'USER':
            text, atts = '', []
            for frag in fragments:
                if frag.get('type') == 'REQUEST' and frag.get('content'):
                    text = frag['content'].strip()
                elif frag.get('type') == 'FILE':
                    for finfo in frag.get('files', []) or []:
                        name = finfo.get('file_name') or 'file'
                        signed = finfo.get('signed_path', '')
                        size = finfo.get('file_size')
                        desc = name + (f' ({size:,} bytes)' if size else '')
                        atts.append(Attachment(
                            filename=name,
                            kind='image' if _IMG_EXTS.search(name) else 'document',
                            data=None,
                            source_url=((signed if signed.startswith('http')
                                         else BASE_URL.rstrip('/') + signed) if signed else ''),
                            description=desc,
                        ))
            if not text and isinstance(msg.get('content'), str):
                text = msg['content']
            if text or atts:
                turns.append(Turn(role='user', text=text, attachments=atts))
        elif role == 'ASSISTANT':
            text = '\n\n'.join(frag.get('content') or '' for frag in fragments
                                 if frag.get('type') == 'RESPONSE').strip()
            if not text and isinstance(msg.get('content'), str):
                text = msg['content']
            thought = bool(msg.get('thinking_content')) or any(
                frag.get('type') == 'THINK' for frag in fragments)
            if text or thought:
                turns.append(Turn(role='model', text=replace_citations(text, citemap),
                                  thought=thought and not text, had_thoughts=thought))

    chat_id = session.get('id', '')
    return Chat(id=chat_id,
                title=session.get('title', ''),
                source_url=f'https://chat.deepseek.com/a/chat/s/{chat_id}',
                turns=turns, provider=PROVIDER)


def materialize_attachments(page, chat: Chat, timeout_ms: int = 60000) -> int:
    """Download attachment bytes via the browser context (cookies + signed
    URLs). Validates responses: DeepSeek signed paths expire, and the server
    then returns the SPA HTML page instead of the file — such responses are
    rejected (attachment stays link-only). Returns files downloaded."""
    ok = 0
    for turn in chat.turns:
        for att in turn.attachments:
            if att.data is not None or not att.source_url:
                continue
            try:
                resp = page.request.get(att.source_url, timeout=timeout_ms)
                if resp.status != 200:
                    continue
                body = resp.body()
                ctype = (resp.headers.get('content-type') or '').lower()
                if not body or 'text/html' in ctype or \
                        body.lstrip()[:15].lower().startswith((b'<!doctype', b'<html')):
                    continue  # expired signed URL -> SPA fallback page
                att.data = body
                ok += 1
            except Exception as e:
                print(f'      attachment download error ({att.filename}): {e}')
    return ok


# ---------------------------------------------------------------------------
# Main (scrape mode)
# ---------------------------------------------------------------------------
class DeepSeek(engine.Provider):
    """What only DeepSeek does; the engine does the rest."""

    name = PROVIDER
    folder = 'Deepseek Chat'
    description = 'Save DeepSeek chats to the database (update by default).'
    pace = 0.0

    def open(self, page):
        ensure_logged_in(page)
        return SimpleNamespace(page=page)

    def announce_listing(self) -> None:
        print('Listing DeepSeek server history (older history may be limited by the service)...')

    def announce_found(self, refs) -> None:
        print(f'Found {len(refs)} chats. Full messages are fetched only when selected.')

    def listing(self, s) -> list[engine.ChatRef]:
        summaries = list_chats(s.page)
        seen = {item['id'] for item in summaries}
        try:  # chats this browser cached locally that the server listing omits
            for cached in read_summaries(s.page):
                if cached['id'] not in seen:
                    summaries.append(cached)
                    seen.add(cached['id'])
        except Exception:
            print('Browser cache could not be read; only server-listed chats are available.')
        return [engine.ChatRef(id=item['id'], title=item.get('title') or '',
                               updated_at=item.get('updated_at'), data=item) for item in summaries]

    def from_url(self, s, url: str) -> engine.ChatRef:
        return engine.ChatRef(id=url.split('/a/chat/s/')[-1].split('?')[0][:40])

    def fetch(self, s, ref: engine.ChatRef) -> engine.Fetched:
        record = fetch_record(s.page, ref.id)
        chat = record_to_chat(record)
        if not chat.visible_turns():
            raise RuntimeError('DeepSeek returned no readable messages; refusing to save an empty chat')
        files = materialize_attachments(s.page, chat)
        return engine.Fetched(chat, raw=record, detail=f'files: {files}')


SPEC = DeepSeek()


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
