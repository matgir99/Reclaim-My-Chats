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
from ..core.manifest import SyncState, write_manifest
from ..core.model import Chat, Turn
from ..core.writer import write_chat

BASE_URL = 'https://chat.deepseek.com/'
PROVIDER = 'deepseek'


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
    """Convert one raw IndexedDB record into a canonical Chat."""
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
            for frag in fragments:
                if frag.get('type') == 'REQUEST' and frag.get('content'):
                    turns.append(Turn(role='user', text=frag['content'].strip()))
                    break
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


# ---------------------------------------------------------------------------
# Main (scrape mode)
# ---------------------------------------------------------------------------
def run(page, records: list[dict], out_dir: Path, resume: bool = False,
        updated_map: dict | None = None) -> list[dict]:
    out_dir = Path(out_dir)
    sync = SyncState(out_dir, PROVIDER)
    results = []
    updated_map = updated_map or {}

    for i, rec in enumerate(records):
        try:
            chat = record_to_chat(rec)
        except Exception as e:
            results.append({'id': '?', 'title': '?', 'ok': False, 'error': str(e)})
            continue
        label = (chat.title or chat.id)[:55]
        if resume and sync.is_unchanged(chat.id, updated_map.get(chat.id)):
            print(f'[{i + 1}/{len(records)}] {label} -> skip (unchanged)')
            continue
        t0 = time.time()
        try:
            stats = write_chat(chat, out_dir)
            sync.mark(chat.id, updated_map.get(chat.id), str(stats['md']))
            users = sum(1 for t in chat.visible_turns() if t.role == 'user')
            results.append({'id': chat.id, 'title': chat.title, 'ok': True,
                            'duration_s': round(time.time() - t0, 1),
                            **{k: stats[k] for k in ('turns', 'chars', 'images', 'docs')}})
            print(f'[{i + 1}/{len(records)}] {label}')
            print(f"  -> {stats['turns']}t ({users}u), {stats['chars']:,} chars")
        except Exception as e:
            print(f'[{i + 1}/{len(records)}] {label} -> FAILED: {e}')
            traceback.print_exc()
            results.append({'id': chat.id, 'title': label, 'ok': False,
                            'error': str(e)})
    sync.save()
    return results


def main(argv=None):
    ap = argparse.ArgumentParser(prog='reclaim scrape deepseek')
    ap.add_argument('--url', help='Scrape single chat URL')
    ap.add_argument('--only', help='Only chats whose title contains this substring')
    ap.add_argument('--start', type=int, default=0)
    ap.add_argument('--limit', type=int, default=0)
    ap.add_argument('--resume', action='store_true')
    ap.add_argument('-o', '--output-dir',
                    default=str(Path(__file__).resolve().parents[2] / 'Deepseek Chat'))
    args = ap.parse_args(argv)
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    from playwright.sync_api import sync_playwright
    started = time.time()
    with sync_playwright() as p:
        ctx, page = browser.launch(p, headless=True)
        try:
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
                if args.only:
                    keep = {s['id'] for s in summaries
                            if args.only.lower() in (s.get('title') or '').lower()}
                    records = [r for r in records
                               if (r.get('data', r).get('chat_session', {})
                                   .get('id')) in keep]
            if args.start:
                records = records[args.start:]
            if args.limit:
                records = records[:args.limit]
            if not records:
                print('No chats matched.')
                return 1

            print(f"\n{'=' * 50}\n  {len(records)} chats\n{'=' * 50}\n")
            results = run(page, records, out_dir, resume=args.resume,
                          updated_map=updated_map)
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
