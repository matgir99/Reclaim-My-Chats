#!/usr/bin/env python3.14
"""
Scrape Google AI Studio conversations with full attachment support.

Captures via API interception:
  - Complete user prompts and model responses (markdown)
  - Model thoughts filtered out

Additionally extracts from DOM:
  - Images (base64 PNG/JPEG saved as files)
  - Document links (Google Drive, PDFs, etc.)
  - Video/audio references

USAGE:
  python3.14 scrape_aistudio.py              # Full library scrape
  python3.14 scrape_aistudio.py --url <URL>  # Single chat
  python3.14 scrape_aistudio.py --retry      # Retry previously failed
"""

import argparse
import base64
import json
import os
import re
import sys
import time
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout
except ImportError:
    print("ERROR: playwright needed. pip3.14 install --break-system-packages playwright")
    sys.exit(1)


# -- Config --
LIBRARY_URL = "https://aistudio.google.com/library"
SCRIPT_DIR = Path(__file__).parent
USER_DATA_DIR = str(SCRIPT_DIR / ".playwright-profile")
OUTPUT_DIR = SCRIPT_DIR  # Output markdown + attachments here

# Thought detection patterns
THOUGHT_INDICATORS = [
    'Research', 'Search', 'Analyzing', 'Reviewing',
    'Investigating', 'Scrutinizing', 'Checking', 'Examining',
    'Refining', 'Evaluating', 'Formulating', 'Planning',
    'Verifying', 'Compiling', 'Synthesizing', 'Gathering',
    'Assessing', 'Pinpointing', 'Exploring', 'Defining',
    'Comparing', 'Processing', 'Generating', 'Creating',
    'Setting', 'Navigating', 'Opening', 'Extracting',
    'Beginning', 'Starting', 'Preparing', 'Organizing',
    'Summarizing', 'Translating', 'Calculating', 'Computing',
    'Identifying', 'Locating', 'Finding', 'Retrieving',
    'Comprehending', 'Understanding', 'Drafting', 'Outlining',
    'Diving', 'Estimating', 'Crunching', 'Tackling',
    'Working', 'Breaking', 'Deciding', 'Looking',
    'Figuring', 'Sorting', 'Filtering', 'Pulling',
    'Focusing', 'Scoping', 'Mapping',
]


# -- Browser helpers --

def clear_cache(page):
    try:
        cdp = page.context.new_cdp_session(page)
        cdp.send('Network.clearBrowserCache')
    except Exception:
        pass


def ensure_logged_in(page):
    page.goto(LIBRARY_URL, wait_until="domcontentloaded", timeout=60000)
    time.sleep(3)
    if "accounts.google.com" in page.url:
        print("\n" + "=" * 50)
        print("  LOG IN to Google in the browser window.")
        print("  Waiting (up to 5 minutes)...")
        print("=" * 50 + "\n")
        try:
            page.wait_for_url("**/aistudio.google.com/**", timeout=300000)
            print("Logged in!\n")
            time.sleep(3)
        except PwTimeout:
            raise RuntimeError("Login timeout")


# -- RPC interception --

def intercept_rpc(page, chat_url: str) -> dict | None:
    """Intercept the ResolveDriveResource RPC to get conversation text."""
    clear_cache(page)
    try:
        with page.expect_response(
            lambda r: 'ResolveDriveResource' in r.url, timeout=45000
        ) as resp_info:
            page.goto(chat_url, wait_until="domcontentloaded", timeout=60000)
        return resp_info.value.json()
    except Exception:
        return None


def parse_rpc(data) -> dict:
    """Parse the RPC response into structured turns."""
    try:
        inner = data[0]
        title = inner[4][0] if len(inner) > 4 and inner[4] and inner[4][0] else ""
        turns = []
        for group in inner[13] if len(inner) > 13 else []:
            for turn in group:
                if not isinstance(turn, list) or len(turn) < 9:
                    continue
                text = turn[0] if isinstance(turn[0], str) else ""
                role = turn[8] if isinstance(turn[8], str) else "unknown"
                if not text.strip():
                    continue
                is_thought = False
                if role == 'model' and text.startswith('**'):
                    m = re.match(r'\*\*(.+?)\*\*', text)
                    if m:
                        first = m.group(1).split()[0] if m.group(1).split() else ""
                        if any(first.startswith(w) for w in THOUGHT_INDICATORS):
                            is_thought = True
                turns.append({"role": role, "text": text, "len": len(text), "is_thought": is_thought})
        return {"title": title, "turns": turns}
    except Exception as e:
        return {"title": "", "turns": [], "error": str(e)}


# -- DOM attachment extraction --

def scroll_all_turns_into_view(page):
    """Scroll through every turn to force virtual scroller to materialize all content."""
    page.evaluate("""
        const s = document.querySelector('.chat-session-content');
        if (s) s.scrollTo(0, 0);
    """)
    time.sleep(0.3)
    
    n = page.evaluate("""
        () => {
            const s = document.querySelector('.chat-session-content');
            return s ? s.querySelectorAll('.chat-turn-container').length : 0;
        }
    """)
    for i in range(n):
        page.evaluate(f"""
            () => {{
                const s = document.querySelector('.chat-session-content');
                const t = s?.querySelectorAll('.chat-turn-container');
                if (t && t[{i}]) t[{i}].scrollIntoView({{block:'center',behavior:'instant'}});
            }}
        """)
        time.sleep(0.3)
    
    page.evaluate("""
        const s = document.querySelector('.chat-session-content');
        if (s) s.scrollTo(0, s.scrollHeight);
    """)
    time.sleep(2)


def extract_attachments(page, chat_slug: str) -> list[dict]:
    """Extract images, documents, and other attachments from the DOM.
    
    Returns list of {type, filename, path, role, description}.
    """
    scroll_all_turns_into_view(page)
    
    attachments = []
    out_dir = OUTPUT_DIR / chat_slug
    out_dir.mkdir(parents=True, exist_ok=True)
    
    result = page.evaluate("""
        () => {
            const session = document.querySelector('.chat-session-content');
            if (!session) return {images: [], documents: []};
            
            const out = {images: [], documents: []};
            
            // --- Images ---
            const imgs = session.querySelectorAll('img');
            for (const img of imgs) {
                const src = img.src || '';
                // Skip tiny UI icons and Google branding
                if (!src || src.includes('google') || src.includes('gstatic') ||
                    src.includes('watermark') || src.includes('logo') ||
                    src.includes('material-symbols') || src.includes('favicon'))
                    continue;
                if (img.naturalWidth < 50 || img.naturalHeight < 50) continue;
                
                // Find parent turn for role
                let turn = img.parentElement;
                while (turn && !turn.className?.includes?.('chat-turn-container'))
                    turn = turn.parentElement;
                const role = turn && turn.className.includes('user') ? 'user' : 'model';
                
                out.images.push({
                    src: src,
                    alt: img.alt || '',
                    w: img.naturalWidth, h: img.naturalHeight,
                    role
                });
            }
            
            // --- Document links ---
            const anchors = session.querySelectorAll('a');
            for (const a of anchors) {
                const href = a.href || '';
                if (href && (href.includes('drive.google.com') || 
                             href.includes('docs.google.com') ||
                             href.endsWith('.pdf') || href.endsWith('.doc') ||
                             href.endsWith('.docx') || href.includes('/file/'))) {
                    let turn = a.parentElement;
                    while (turn && !turn.className?.includes?.('chat-turn-container'))
                        turn = turn.parentElement;
                    const role = turn && turn.className.includes('user') ? 'user' : 'model';
                    out.documents.push({
                        href, role,
                        text: (a.innerText || '').trim().substring(0, 100)
                    });
                }
            }
            
            return out;
        }
    """)
    
    # Save images
    for i, img in enumerate(result.get('images', [])):
        src = img['src']
        if src.startswith('data:'):
            try:
                header, b64data = src.split(',', 1)
                mime = header.split(':')[1].split(';')[0]
                ext = mime.split('/')[1]
                data = base64.b64decode(b64data)
                filename = f"image_{i+1}.{ext}"
                filepath = out_dir / filename
                with open(filepath, 'wb') as f:
                    f.write(data)
                attachments.append({
                    'type': 'image',
                    'filename': filename,
                    'path': str(filepath),
                    'role': img['role'],
                    'size': len(data),
                    'dimensions': f"{img['w']}x{img['h']}",
                    'alt': img.get('alt', ''),
                })
            except Exception as e:
                print(f"    Image save error: {e}")
        elif src.startswith('http'):
            attachments.append({
                'type': 'image_url',
                'filename': '',
                'path': src,
                'role': img['role'],
                'size': 0,
                'dimensions': f"{img['w']}x{img['h']}",
            })
    
    # Save document references
    for doc in result.get('documents', []):
        attachments.append({
            'type': 'document_link',
            'filename': '',
            'path': doc['href'],
            'role': doc['role'],
            'description': doc.get('text', ''),
        })
    
    return attachments


# -- Library scraping --

def get_chat_list(page) -> list[dict]:
    """Scrape all chat links from the library."""
    print("Scanning library for chats...")
    for _ in range(30):
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        time.sleep(1.5)
    links = page.evaluate("""
        () => {
            const R = [], S = new Set();
            document.querySelectorAll('a[href*="/prompts/"]').forEach(a => {
                const p = a.href.split('/prompts/');
                if (p.length < 2) return;
                const id = p[1].split('?')[0].split('#')[0];
                if (id && !S.has(id) && id !== 'new_chat') {
                    S.add(id);
                    R.push({id, url: a.href.split('?')[0].split('#')[0],
                            title: (a.innerText||'').trim().substring(0,200)||'(no title)'});
                }
            });
            return R;
        }
    """)
    seen = set()
    unique = []
    for l in links:
        if l['url'] not in seen:
            seen.add(l['url'])
            unique.append(l)
    print(f"Found {len(unique)} chats.")
    return unique


# -- Saving --

def slugify(title: str) -> str:
    """Create a filesystem-safe slug from a title."""
    return re.sub(r'[^\w\s-]', '', title)[:60].strip()


def save_chat(turns, attachments, chat_info, out_dir: Path):
    """Save markdown file with image references."""
    title = chat_info.get('title', '')
    slug = slugify(title) or chat_info['id'][:20]
    
    # Create subfolder for chat
    chat_dir = out_dir / slug
    chat_dir.mkdir(parents=True, exist_ok=True)
    
    md_path = chat_dir / f"{slug}.md"
    counter = 1
    while md_path.exists():
        md_path = chat_dir / f"{slug}_{counter}.md"
        counter += 1
    
    ts = time.strftime('%Y-%m-%d %H:%M:%S')
    real_turns = [t for t in turns if not t.get('is_thought')]
    
    # Move attachment files into chat folder
    img_attachments = [a for a in attachments if a['type'] == 'image']
    doc_attachments = [a for a in attachments if a['type'] == 'document_link']
    
    with open(md_path, 'w') as f:
        f.write(f"# {title}\n\n")
        f.write(f"**Source:** {chat_info['url']}\n\n")
        f.write(f"**Scraped:** {ts}\n\n")
        
        if img_attachments:
            f.write(f"**Attachments:** {len(img_attachments)} image(s)\n\n")
        if doc_attachments:
            f.write(f"**Documents:** {len(doc_attachments)} link(s)\n\n")
        
        f.write("---\n\n")
        
        for t in real_turns:
            label = {"user": "User", "model": "Model"}.get(t['role'], t['role'])
            f.write(f"## {label}\n\n{t['text']}\n\n---\n\n")
    
    return md_path


# -- Main --

def scrape_one(page, chat: dict):
    """Scrape a single chat: text via API + attachments via DOM."""
    print(f"  [{chat['title'][:60]}]")
    
    # Step 1: API interception for text
    data = intercept_rpc(page, chat['url'])
    if data is None:
        print("    API failed, using DOM fallback...")
        # DOM fallback: reload page and extract text
        clear_cache(page)
        page.goto(chat['url'], wait_until="domcontentloaded", timeout=60000)
        time.sleep(8)
        scroll_all_turns_into_view(page)
        
        # Extract title from DOM
        title = page.evaluate("""
            () => {
                const h1 = document.querySelector('h1');
                if (h1) return h1.innerText.trim();
                const toolbar = document.querySelector('.page-title, .title-tokencount-container');
                if (toolbar) return toolbar.innerText.split('\\n')[0].trim();
                return '';
            }
        """)
        
        # Extract turns from DOM
        turns_raw = page.evaluate("""
            () => {
                const session = document.querySelector('.chat-session-content');
                if (!session) return [];
                const containers = session.querySelectorAll('.chat-turn-container');
                const turns = [];
                const seen = new Set();
                for (const turn of containers) {
                    const cls = (turn.className || '').toString().toLowerCase();
                    let role = cls.includes(' user') ? 'user' : (cls.includes(' model') ? 'model' : 'unknown');
                    const chunks = turn.querySelectorAll('ms-text-chunk');
                    const parts = [];
                    for (const c of chunks) {
                        const t = (c.textContent || '').trim();
                        if (t && t.length > 5) parts.push(t);
                    }
                    let text = parts.join('\\n\\n');
                    // Deduplicate
                    if (text && !seen.has(text.substring(0, 100))) {
                        seen.add(text.substring(0, 100));
                        turns.push({role, text, len: text.length, is_thought: false});
                    }
                }
                return turns;
            }
        """)
        
        parsed = {"title": title or chat.get('title', ''), "turns": turns_raw}
        api_ok = False
    else:
        parsed = parse_rpc(data)
        api_ok = True
    
    turns = parsed.get('turns', [])
    if not turns and not api_ok:
        # DOM fallback for text
        clear_cache(page)
        page.goto(chat['url'], wait_until="domcontentloaded", timeout=60000)
        time.sleep(5)
        scroll_all_turns_into_view(page)
        raw = page.evaluate("""
            () => {
                const s = document.querySelector('.chat-session-content');
                return s ? s.innerText : '';
            }
        """)
        turns = [{"role": "model", "text": raw, "len": len(raw), "is_thought": False}] if raw else []
    
    chat['title'] = parsed.get('title') or chat.get('title', '')
    
    # Step 2: DOM-based attachment extraction
    attachments = []
    if api_ok:
        # Page is already loaded from RPC interception, just wait a bit
        time.sleep(3)
    attachments = extract_attachments(page, slugify(chat['title']) or chat['id'][:20])
    
    # Move images into the chat folder
    slug = slugify(chat['title']) or chat['id'][:20]
    chat_dir = OUTPUT_DIR / slug
    chat_dir.mkdir(parents=True, exist_ok=True)
    
    # Move any extracted images into chat dir
    tmp_dir = OUTPUT_DIR / slug
    for a in attachments:
        if a['type'] == 'image' and a.get('path'):
            src = Path(a['path'])
            if src.parent != chat_dir:
                dst = chat_dir / src.name
                if src.exists():
                    src.rename(dst)
                    a['path'] = str(dst)
    
    return chat, turns, attachments


def main():
    ap = argparse.ArgumentParser(description="Scrape Google AI Studio conversations")
    ap.add_argument("--url", help="Scrape a single chat URL")
    ap.add_argument("--retry", action="store_true", help="Retry previously failed chats")
    ap.add_argument("-o", "--output-dir", default=str(OUTPUT_DIR))
    args = ap.parse_args()

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    os.makedirs(USER_DATA_DIR, exist_ok=True)

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=USER_DATA_DIR, headless=False,
            executable_path="/usr/bin/google-chrome-stable",
            args=["--disable-blink-features=AutomationControlled",
                  "--disable-features=AutomationControlled",
                  "--disable-dev-shm-usage"],
            viewport={"width": 1400, "height": 900},
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        ensure_logged_in(page)

        if args.url:
            chats = [{'id': args.url.split('/')[-1][:40], 'url': args.url, 'title': ''}]
        else:
            chats = get_chat_list(page)

        print(f"\n{'=' * 50}")
        print(f"  Scraping {len(chats)} chats...")
        print(f"{'=' * 50}\n")

        done = 0
        failed = 0
        total_imgs = 0
        total_docs = 0

        for i, chat in enumerate(chats):
            try:
                chat, turns, attachments = scrape_one(page, chat)
                
                img_count = sum(1 for a in attachments if a['type'] == 'image')
                doc_count = sum(1 for a in attachments if a['type'] == 'document_link')
                total_imgs += img_count
                total_docs += doc_count
                
                real = [t for t in turns if not t.get('is_thought')]
                u = sum(1 for t in real if t['role'] == 'user')
                m = sum(1 for t in real if t['role'] == 'model')
                chars = sum(t['len'] for t in real)
                
                extras = ""
                if img_count: extras += f", {img_count} img"
                if doc_count: extras += f", {doc_count} doc"
                
                print(f"[{i+1}/{len(chats)}] -> {len(real)}t ({u}u/{m}m), {chars:,} chars{extras}")
                
                md = save_chat(turns, attachments, chat, out_dir)
                print(f"         -> {md.relative_to(out_dir)}")
                done += 1
                
            except Exception as e:
                print(f"[{i+1}/{len(chats)}] -> FAILED: {e}")
                failed += 1
            
            time.sleep(0.5)

        print(f"\n{'=' * 50}")
        print(f"  Done: {done} scraped, {failed} failed.")
        print(f"  Images: {total_imgs}, Documents: {total_docs}")
        print(f"  Output: {out_dir}")
        print(f"{'=' * 50}")

        ctx.close()


if __name__ == "__main__":
    main()
