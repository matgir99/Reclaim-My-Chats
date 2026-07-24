#!/usr/bin/env python3.14
"""
Scrape DeepSeek Chat conversations from IndexedDB.

DeepSeek stores all chat history in the browser's IndexedDB ('deepseek-chat' 
database, 'history-message' store). Each record contains the full conversation
in raw markdown (including proper LaTeX delimiters), fragment types (REQUEST,
THINK, RESPONSE, SEARCH), and citation metadata with URLs.

This is the clean download path — equivalent to Google AI Studio's RPC approach.
No DOM scraping needed.

USAGE:
  python3.14 scrape_deepseek.py              # Full library from IndexedDB
  python3.14 scrape_deepseek.py --url <URL>  # Single chat (URL is used to find
                                               the local data by ID)

Each chat saved in its own folder: <slug>/<slug>.md
"""

import argparse
import os
import re
import sys
import time
import traceback
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout
except ImportError:
    print("ERROR: playwright needed.")
    sys.exit(1)

# -- Config --
BASE_URL = "https://chat.deepseek.com/"
SCRIPT_DIR = Path(__file__).resolve().parent
USER_DATA_DIR = str(SCRIPT_DIR.parent / ".playwright-profile")


# -- Browser & IndexedDB --
def ensure_logged_in(page):
    page.goto(BASE_URL, wait_until="domcontentloaded", timeout=60000)
    time.sleep(3)
    if 'sign_in' in page.url or 'login' in page.url.lower():
        print("\n" + "=" * 50)
        print("  LOG IN to DeepSeek in the browser window.")
        print("  Waiting (up to 5 minutes)...")
        print("=" * 50 + "\n")
        try:
            page.wait_for_function(
                """() => document.querySelector('.ds-message, textarea') !== null""",
                timeout=300000
            )
            print("Logged in!\n")
            time.sleep(3)
        except PwTimeout:
            raise RuntimeError("Login timeout")


def read_all_from_indexeddb(page) -> list[dict]:
    """Read all chat records from IndexedDB's history-message store."""
    # Wait for IndexedDB to be populated
    time.sleep(2)
    
    records = page.evaluate("""() => {
        return new Promise((resolve, reject) => {
            const req = indexedDB.open('deepseek-chat');
            req.onsuccess = (e) => {
                const db = e.target.result;
                const tx = db.transaction('history-message', 'readonly');
                const store = tx.objectStore('history-message');
                const getAll = store.getAll();
                getAll.onsuccess = () => {
                    db.close();
                    // Return minimal data; we'll extract text later
                    const summaries = getAll.result.map(rec => {
                        const session = rec.data?.chat_session || {};
                        return {
                            id: session.id || 'unknown',
                            title: session.title || '',
                            updated_at: session.updated_at,
                            model_type: session.model_type || '',
                        };
                    });
                    resolve(summaries);
                };
                getAll.onerror = () => {
                    db.close();
                    reject('getAll failed');
                };
            };
            req.onerror = () => reject('Cannot open IndexedDB');
        });
    }""")
    return records


def read_single_chat_from_indexeddb(page, chat_id: str) -> dict | None:
    """Read a single chat record by its UUID."""
    result = page.evaluate("""(chatId) => {
        return new Promise((resolve, reject) => {
            const req = indexedDB.open('deepseek-chat');
            req.onsuccess = (e) => {
                const db = e.target.result;
                const tx = db.transaction('history-message', 'readonly');
                const store = tx.objectStore('history-message');
                const getAll = store.getAll();
                getAll.onsuccess = () => {
                    const records = getAll.result;
                    const found = records.find(r => r.data?.chat_session?.id === chatId);
                    db.close();
                    if (found) {
                        resolve(found);
                    } else {
                        resolve(null);
                    }
                };
                getAll.onerror = () => { db.close(); reject('getAll failed'); };
            };
            req.onerror = () => reject('Cannot open IndexedDB');
        });
    }""", chat_id)
    return result


# -- Formatting --
def slugify(title: str) -> str:
    return re.sub(r'[^\w\s-]', '', title)[:60].strip()


def build_citation_map(fragments: list[dict]) -> dict[int, dict]:
    """Build citation index -> {url, title} map from SEARCH fragments."""
    citemap = {}
    for frag in fragments:
        if frag.get('type') == 'SEARCH':
            for result in frag.get('results', []):
                idx = result.get('cite_index')
                if idx:
                    citemap[idx] = {
                        'url': result.get('url', ''),
                        'title': result.get('title', ''),
                        'snippet': result.get('snippet', ''),
                    }
    return citemap


def replace_citations(text: str, citemap: dict[int, dict]) -> str:
    """Replace [citation:N] with [-N](url) in markdown text."""
    def replacer(match):
        n = int(match.group(1))
        info = citemap.get(n, {})
        url = info.get('url', '')
        if url:
            return f'[-{n}]({url})'
        return f'[-{n}]'
    return re.sub(r'\[citation:(\d+)\]', replacer, text)


def process_chat_record(record: dict) -> dict:
    """Process a raw IndexedDB record into structured messages."""
    data = record.get('data', record)
    session = data.get('chat_session', {})
    messages = data.get('chat_messages', [])
    
    title = session.get('title', '')
    chat_id = session.get('id', '')
    
    turns = []
    # Collect all SEARCH fragments across the conversation for citation mapping
    all_fragments = []
    for msg in messages:
        all_fragments.extend(msg.get('fragments', []))
    
    citemap = build_citation_map(all_fragments)
    
    for msg in messages:
        role = msg.get('role', '').upper()
        fragments = msg.get('fragments', [])
        
        if role == 'USER':
            # User messages have a REQUEST fragment
            for frag in fragments:
                if frag.get('type') == 'REQUEST' and frag.get('content'):
                    turns.append({
                        'role': 'user',
                        'text': frag['content'].strip(),
                    })
                    break
        
        elif role == 'ASSISTANT':
            # Only extract RESPONSE fragments; skip THINK (model reasoning)
            for frag in fragments:
                if frag.get('type') == 'RESPONSE':
                    answer_text = frag.get('content', '')
                    if answer_text:
                        answer_text = replace_citations(answer_text, citemap)
                    turns.append({
                        'role': 'assistant',
                        'text': answer_text,
                    })
    
    return {
        'id': chat_id,
        'title': title,
        'turns': turns,
        'citation_count': len(citemap),
    }


def save_chat(processed: dict, out_dir: Path) -> Path:
    title = processed['title']
    slug = slugify(title) or processed['id'][:20]
    chat_dir = out_dir / slug
    chat_dir.mkdir(parents=True, exist_ok=True)
    
    md = chat_dir / f"{slug}.md"
    c = 1
    while md.exists():
        md = chat_dir / f"{slug}_{c}.md"; c += 1
    
    ts = time.strftime('%Y-%m-%d %H:%M:%S')
    url = f"https://chat.deepseek.com/a/chat/s/{processed['id']}"
    
    with open(md, 'w') as f:
        f.write(f"# {title}\n\n")
        f.write(f"**Source:** {url}\n\n")
        f.write(f"**Scraped:** {ts}\n\n")
        f.write("---\n\n")
        
        for t in processed['turns']:
            if t['role'] == 'user':
                f.write(f"## User\n\n{t['text']}\n\n---\n\n")
            else:
                if t.get('text'):
                    f.write(f"## Assistant\n\n{t['text']}\n\n---\n\n")
    
    return md


# -- Main --
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", help="Scrape single chat URL (uses local IndexedDB data)")
    ap.add_argument("-o", "--output-dir", default=str(SCRIPT_DIR))
    args = ap.parse_args()
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    os.makedirs(USER_DATA_DIR, exist_ok=True)
    
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=USER_DATA_DIR, headless=True,
            executable_path="/usr/bin/google-chrome-stable",
            args=["--disable-blink-features=AutomationControlled",
                  "--disable-features=AutomationControlled", "--disable-dev-shm-usage"],
            viewport={"width": 1400, "height": 900},
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        ensure_logged_in(page)
        
        if args.url:
            # Extract chat ID from URL
            chat_id = args.url.split('/a/chat/s/')[-1].split('?')[0][:40]
            print(f"Looking up chat {chat_id} in IndexedDB...")
            rec = read_single_chat_from_indexeddb(page, chat_id)
            if rec is None:
                print(f"ERROR: Chat {chat_id} not found in local IndexedDB.")
                print("Make sure you've opened this chat in the browser before.")
                ctx.close()
                sys.exit(1)
            records = [rec]
        else:
            print("Reading chat list from IndexedDB...")
            summaries = read_all_from_indexeddb(page)
            print(f"Found {len(summaries)} chats. Loading full records...")
            
            # Read all full records
            records = page.evaluate("""() => {
                return new Promise((resolve, reject) => {
                    const req = indexedDB.open('deepseek-chat');
                    req.onsuccess = (e) => {
                        const db = e.target.result;
                        const tx = db.transaction('history-message', 'readonly');
                        const store = tx.objectStore('history-message');
                        const getAll = store.getAll();
                        getAll.onsuccess = () => {
                            db.close();
                            resolve(getAll.result);
                        };
                        getAll.onerror = () => { db.close(); reject('getAll failed'); };
                    };
                    req.onerror = () => reject('Cannot open IndexedDB');
                });
            }""")
        
        if not records:
            print("No chats found.")
            ctx.close()
            return
        
        print(f"\n{'='*50}\n  Processing {len(records)} chats\n{'='*50}\n")
        
        done = failed = 0
        total_chars = 0
        
        for i, rec in enumerate(records):
            processed = process_chat_record(rec)
            label = (processed['title'] or processed['id'])[:60]
            
            try:
                md_path = save_chat(processed, out_dir)
                
                users = sum(1 for t in processed['turns'] if t['role'] == 'user')
                assistants = sum(1 for t in processed['turns'] if t['role'] == 'assistant')
                chars = sum(len(t['text']) for t in processed['turns'])
                total_chars += chars
                cit = f", {processed['citation_count']} citations" if processed['citation_count'] else ""
                
                print(f"[{i+1}/{len(records)}] {label}")
                print(f"  -> {len(processed['turns'])}t ({users}u/{assistants}a), {chars:,} chars{cit}")
                print(f"     saved: {md_path}")
                done += 1
            except Exception as e:
                print(f"  -> FAILED: {e}")
                traceback.print_exc()
                failed += 1
        
        print(f"\n{'='*50}")
        print(f"  Done: {done} ok, {failed} failed")
        print(f"  Total text: {total_chars:,} chars")
        print(f"  Output: {out_dir}")
        print(f"{'='*50}")
        ctx.close()


if __name__ == "__main__":
    main()
