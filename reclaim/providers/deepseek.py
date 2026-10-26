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

import argparse
import json
import os
import re
import time
import traceback
from pathlib import Path
from urllib.parse import urlencode

from ..core import browser, config
from ..core.events import EventSink, emit
from ..core.manifest import SyncState, print_dry_run, write_manifest
from ..core.model import Attachment, Chat, Turn
from ..core.progress import progress
from ..core.writer import write_chat, write_raw

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
    return {status: response.status, text: await response.text()};
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
    for attempt in range(3):
        response = page.evaluate(_GET_JS, {'url': url})
        status = response['status']
        if status in (429, 500, 502, 503, 504) and attempt < 2:
            time.sleep(2 + attempt * 3)
            continue
        if status != 200:
            raise RuntimeError(f'DeepSeek history API HTTP {status}; check your login and retry')
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
    raise RuntimeError('DeepSeek history API retries exhausted')


def list_chats(page) -> list[dict]:
    """Read the two server listings; never accept a stuck/incomplete page."""
    chats, seen = [], set()
    for pinned in ('false', 'true'):
        cursor, seen_cursors = None, set()
        while True:
            params: dict[str, str | int | float] = {'lte_cursor.pinned': pinned}
            if cursor is not None:
                params['lte_cursor.updated_at'] = cursor
            data = _api_get(page, 'chat_session/fetch_page', params)
            batch = data.get('chat_sessions')
            if not isinstance(batch, list) or not isinstance(data.get('has_more'), bool):
                raise RuntimeError('unexpected DeepSeek conversation list shape')
            for item in batch:
                if not isinstance(item, dict) or not item.get('id'):
                    raise RuntimeError('unexpected DeepSeek conversation row')
                if item['id'] not in seen:
                    seen.add(item['id'])
                    chats.append(item)
            if not data['has_more']:
                break
            cursor = data.get('lte_cursor')
            if cursor is None and batch:
                cursor = batch[-1].get('updated_at')
            if (not batch or not isinstance(cursor, (int, float))
                    or cursor in seen_cursors):
                raise RuntimeError('DeepSeek conversation pagination made no progress; archive may be incomplete')
            seen_cursors.add(cursor)
    return chats


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
def run(page, records: list[dict], out_dir: Path,
        skip_unchanged: bool = False, updated_map: dict | None = None,
        save_raw: bool = True, log: bool = False,
        sink: EventSink | None = None, fetch_missing: bool = False) -> list[dict]:
    """Archive the given records. Returns per-chat results for the manifest.

    Per-chat lines print only with log=True (default output is
    summary-level; failures always print)."""
    out_dir = Path(out_dir)
    sync = SyncState(out_dir, PROVIDER)
    results = []
    updated_map = updated_map or {}
    t_run = time.time()

    for i, rec in enumerate(records):
        try:
            chat = record_to_chat(rec)
        except Exception as e:
            results.append({'id': '?', 'title': '?', 'ok': False, 'error': str(e)})
            emit(sink, 'error', PROVIDER, '?', i + 1, len(records), str(e))
            continue
        label = (chat.title or chat.id)[:55]
        emit(sink, 'chat_started', PROVIDER, label, i + 1, len(records))
        if log:
            print(progress(i + 1, len(records), t_run))
        if skip_unchanged and sync.is_unchanged(chat.id,
                                                updated_map.get(chat.id)):
            if log:
                print(f'[{i + 1}/{len(records)}] {label} -> skip (unchanged)')
            emit(sink, 'chat_skipped', PROVIDER, label, i + 1, len(records))
            continue
        t0 = time.time()
        try:
            if fetch_missing:
                rec = fetch_record(page, chat.id)
                chat = record_to_chat(rec)
                if not chat.visible_turns():
                    raise RuntimeError('DeepSeek returned no readable messages; refusing an empty archive')
            n_files = materialize_attachments(page, chat)
            stats = write_chat(chat, out_dir)
            if save_raw:
                try:
                    write_raw(stats['dir'], rec)
                except Exception:
                    pass
            sync.mark(chat.id, updated_map.get(chat.id), str(stats['md']))
            results.append({'id': chat.id, 'title': chat.title, 'ok': True,
                            'duration_s': round(time.time() - t0, 1),
                            **{k: stats[k] for k in ('turns', 'chars', 'images', 'docs')}})
            emit(sink, 'chat_completed', PROVIDER, chat.title, i + 1, len(records))
            if log:
                extra = f", {stats['images']} img" if stats['images'] else ''
                extra += f", {stats['docs']} doc" if stats['docs'] else ''
                print(f'[{i + 1}/{len(records)}] {label} -> {stats["turns"]}t, '
                      f'{stats["chars"]:,} chars{extra}')
                print(f'    files: {n_files} · {time.time() - t0:.1f}s')
        except Exception as e:
            print(f'[{i + 1}/{len(records)}] {label} -> FAILED: {e}')
            traceback.print_exc()
            results.append({'id': chat.id, 'title': label, 'ok': False,
                            'error': str(e)})
            emit(sink, 'error', PROVIDER, label, i + 1, len(records), str(e))
    sync.save()
    return results


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog='reclaim deepseek',
        description='Archive DeepSeek Chat history (update by default).')
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
                    default=str(config.archive_root() / 'Deepseek Chat'))
    return ap


def _rec_id(rec: dict) -> str:
    try:
        return rec.get('data', rec).get('chat_session', {}).get('id') or '?'
    except Exception:
        return '?'


def _rec_title(rec: dict) -> str:
    try:
        return record_to_chat(rec).title
    except Exception:
        return '?'


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


def run_session(page, args, sink: EventSink | None = None) -> int:
    """Provider session on an already-launched page; shared by main()
    (own browser) and `reclaim all` (one shared browser)."""
    out_dir = Path(args.output_dir)
    started = time.time()
    ensure_logged_in(page)
    if args.url:
        chat_id = args.url.split('/a/chat/s/')[-1].split('?')[0][:40]
        summaries = [{'id': chat_id, 'title': chat_id}]
    else:
        print('Listing DeepSeek server history (older history may be limited by the service)...')
        summaries = list_chats(page)
        seen = {s['id'] for s in summaries}
        try:
            for cached in read_summaries(page):
                if cached['id'] not in seen:
                    summaries.append(cached)
                    seen.add(cached['id'])
        except Exception:
            print('Browser cache could not be read; only server-listed chats are available.')
        print(f'Found {len(summaries)} chats. Full messages are fetched only when selected.')
    updated_map = {s['id']: s.get('updated_at') for s in summaries}
    records = [{'data': {'chat_session': s}} for s in summaries]
    if args.title:
        records = [r for r in records
                   if args.title.lower() in _rec_title(r).lower()]
    if args.skip:
        records = records[args.skip:]
    if args.limit:
        records = records[:args.limit]
    if not records:
        print('No chats matched.')
        return 1
    if args.list:
        for n, r in enumerate(records, 1):
            print(f'{n}. {_rec_title(r)}')
        print(f'-- {len(records)} chat(s)')
        return 0
    skip_unchanged = not args.rebuild and not args.title
    if args.dry_run:
        sync = SyncState(out_dir, PROVIDER, migrate=False)
        view = [{'id': _rec_id(r), 'title': _rec_title(r)}
                for r in records]
        return print_dry_run(view, updated_map, sync, skip_unchanged,
                             log=args.log, sink=sink, provider=PROVIDER)

    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"\n{'=' * 50}\n  {len(records)} chats\n{'=' * 50}\n")
    results = run(page, records, out_dir, skip_unchanged=skip_unchanged,
                  updated_map=updated_map, save_raw=not args.no_raw,
                  log=args.log, sink=sink, fetch_missing=True)
    manifest = write_manifest(out_dir, PROVIDER, results, started)
    ok = sum(1 for r in results if r.get('ok'))
    print(f"\n{'=' * 50}")
    print(f'  Done: {ok} ok, {len(results) - ok} failed')
    print(f'  Manifest: {manifest}')
    print(f"{'=' * 50}")
    return 0 if ok == len(results) else 2


if __name__ == '__main__':
    raise SystemExit(main())
