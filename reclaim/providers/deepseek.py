"""DeepSeek Chat provider (scrape mode) — IndexedDB architecture.

DeepSeek stores all chat history in the browser's IndexedDB ('deepseek-chat'
database, 'history-message' store). Records contain raw markdown (LaTeX
delimiters intact), fragment types (REQUEST, THINK, RESPONSE, SEARCH) and
citation metadata. Reading IndexedDB yields the exact model-produced text —
no DOM scraping needed.

Fragment handling: REQUEST (user) and RESPONSE (assistant) are kept; THINK
(model reasoning) is dropped; SEARCH fragments provide citation URLs mapped
from [citation:N] to [-N](url).
"""

from __future__ import annotations

import argparse
import re
import time
import traceback
from pathlib import Path

from ..core import browser
from ..core.manifest import SyncState, print_dry_run, write_manifest
from ..core.model import Attachment, Chat, Turn
from ..core.progress import progress
from ..core.writer import write_chat, write_raw

BASE_URL = 'https://chat.deepseek.com/'
PROVIDER = 'deepseek'
_IMG_EXTS = re.compile(r'\.(png|jpe?g|gif|webp)$', re.I)


# ---------------------------------------------------------------------------
# Login + IndexedDB reads
# ---------------------------------------------------------------------------
def ensure_logged_in(page):
    page.goto(BASE_URL, wait_until='domcontentloaded', timeout=60000)
    time.sleep(3)
    if 'sign_in' in page.url or 'login' in page.url.lower():
        browser.interactive_login(page, 'login', '**/chat.deepseek.com/**',
                                  'DeepSeek')


def read_summaries(page) -> list[dict]:
    """Lightweight list of all chats (id, title, updated_at)."""
    time.sleep(2)
    return page.evaluate("""() => {
        return new Promise((resolve, reject) => {
            const req = indexedDB.open('deepseek-chat');
            req.onsuccess = (e) => {
                const db = e.target.result;
                const tx = db.transaction('history-message', 'readonly');
                const store = tx.objectStore('history-message');
                const getAll = store.getAll();
                getAll.onsuccess = () => {
                    db.close();
                    resolve(getAll.result.map(rec => {
                        const s = rec.data?.chat_session || {};
                        return {id: s.id || 'unknown', title: s.title || '',
                                updated_at: s.updated_at,
                                model_type: s.model_type || ''};
                    }));
                };
                getAll.onerror = () => { db.close(); reject('getAll failed'); };
            };
            req.onerror = () => reject('Cannot open IndexedDB');
        });
    }""")


def read_all_records(page) -> list[dict]:
    """Full records for every chat."""
    return page.evaluate("""() => {
        return new Promise((resolve, reject) => {
            const req = indexedDB.open('deepseek-chat');
            req.onsuccess = (e) => {
                const db = e.target.result;
                const tx = db.transaction('history-message', 'readonly');
                const store = tx.objectStore('history-message');
                const getAll = store.getAll();
                getAll.onsuccess = () => { db.close(); resolve(getAll.result); };
                getAll.onerror = () => { db.close(); reject('getAll failed'); };
            };
            req.onerror = () => reject('Cannot open IndexedDB');
        });
    }""")


def read_single_record(page, chat_id: str) -> dict | None:
    return page.evaluate("""(chatId) => {
        return new Promise((resolve, reject) => {
            const req = indexedDB.open('deepseek-chat');
            req.onsuccess = (e) => {
                const db = e.target.result;
                const tx = db.transaction('history-message', 'readonly');
                const store = tx.objectStore('history-message');
                const getAll = store.getAll();
                getAll.onsuccess = () => {
                    const found = getAll.result.find(r => r.data?.chat_session?.id === chatId);
                    db.close();
                    resolve(found || null);
                };
                getAll.onerror = () => { db.close(); reject('getAll failed'); };
            };
            req.onerror = () => reject('Cannot open IndexedDB');
        });
    }""", chat_id)


# ---------------------------------------------------------------------------
# Record processing (raw IndexedDB record -> canonical Chat)
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
    """Convert one raw IndexedDB record into a canonical Chat.

    FILE fragments (user uploads) become attachments whose ``source_url`` is
    the signed download path; bytes are fetched later by
    :func:`materialize_attachments` (needs the live page)."""
    data = record.get('data', record)
    session = data.get('chat_session', {})
    messages = data.get('chat_messages', [])

    all_fragments = []
    for msg in messages:
        all_fragments.extend(msg.get('fragments', []))
    citemap = build_citation_map(all_fragments)

    turns = []
    for msg in messages:
        role = msg.get('role', '').upper()
        fragments = msg.get('fragments', [])
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
                            source_url=(signed if signed.startswith('http')
                                        else BASE_URL.rstrip('/') + signed),
                            description=desc,
                        ))
            if text or atts:
                turns.append(Turn(role='user', text=text, attachments=atts))
        elif role == 'ASSISTANT':
            for frag in fragments:
                if frag.get('type') == 'RESPONSE':
                    text = frag.get('content', '')
                    if text:
                        text = replace_citations(text, citemap)
                    turns.append(Turn(role='model', text=text))

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
        save_raw: bool = True, log: bool = False) -> list[dict]:
    """Archive the given records. Returns per-chat results for the manifest."""
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
            continue
        label = (chat.title or chat.id)[:55]
        if log:
            print(progress(i + 1, len(records), t_run))
        if skip_unchanged and sync.is_unchanged(chat.id,
                                                updated_map.get(chat.id)):
            print(f'[{i + 1}/{len(records)}] {label} -> skip (unchanged)')
            continue
        t0 = time.time()
        try:
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
            extra = f", {stats['images']} img" if stats['images'] else ''
            extra += f", {stats['docs']} doc" if stats['docs'] else ''
            print(f'[{i + 1}/{len(records)}] {label} -> {stats["turns"]}t, '
                  f'{stats["chars"]:,} chars{extra}')
            if log:
                print(f'    files: {n_files} · {time.time() - t0:.1f}s')
        except Exception as e:
            print(f'[{i + 1}/{len(records)}] {label} -> FAILED: {e}')
            traceback.print_exc()
            results.append({'id': chat.id, 'title': label, 'ok': False,
                            'error': str(e)})
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
                                / 'Deepseek Chat'))
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
        ctx, page = browser.launch(p, headless=True)
        try:
            return run_session(page, args)
        finally:
            ctx.close()


def run_session(page, args) -> int:
    """Provider session on an already-launched page; shared by main()
    (own headless browser) and `reclaim all` (one shared browser)."""
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    started = time.time()
    ensure_logged_in(page)
    if args.url:
        chat_id = args.url.split('/a/chat/s/')[-1].split('?')[0][:40]
        rec = read_single_record(page, chat_id)
        if rec is None:
            print(f'ERROR: chat {chat_id} not found in IndexedDB.')
            return 1
        records, updated_map = [rec], {}
    else:
        print('Reading chat list from IndexedDB...')
        summaries = read_summaries(page)
        print(f'Found {len(summaries)} chats. Loading full records...')
        updated_map = {s['id']: s.get('updated_at') for s in summaries}
        records = read_all_records(page)
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
        return print_dry_run(view, updated_map, sync, skip_unchanged)

    print(f"\n{'=' * 50}\n  {len(records)} chats\n{'=' * 50}\n")
    results = run(page, records, out_dir, skip_unchanged=skip_unchanged,
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
