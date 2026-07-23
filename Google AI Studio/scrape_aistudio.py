#!/usr/bin/env python3.14
"""
Scrape Google AI Studio conversations — text, images, and documents.

Two-pass approach:
  Pass 1 (fast): Intercept RPC for text. Check Content-Length header.
  Pass 2 (slow, only if needed): Scroll DOM to extract images/documents.

Each chat saved in its own folder: <title>/<title>.md + images + docs

USAGE:
  python3.14 scrape_aistudio.py              # Full library
  python3.14 scrape_aistudio.py --url <URL>  # Single chat
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

# If RPC response is larger than this, do DOM pass for attachments
LARGE_RESPONSE_THRESHOLD = 1_000_000  # 1 MB

THOUGHT_INDICATORS = [
    'Research', 'Search', 'Analyzing', 'Reviewing', 'Investigating',
    'Scrutinizing', 'Checking', 'Examining', 'Refining', 'Evaluating',
    'Formulating', 'Planning', 'Verifying', 'Compiling', 'Synthesizing',
    'Gathering', 'Assessing', 'Pinpointing', 'Exploring', 'Defining',
    'Comparing', 'Processing', 'Generating', 'Creating', 'Setting',
    'Navigating', 'Opening', 'Extracting', 'Beginning', 'Starting',
    'Preparing', 'Organizing', 'Summarizing', 'Translating', 'Calculating',
    'Computing', 'Identifying', 'Locating', 'Finding', 'Retrieving',
    'Comprehending', 'Understanding', 'Drafting', 'Outlining', 'Diving',
    'Estimating', 'Crunching', 'Tackling', 'Working', 'Breaking',
    'Deciding', 'Looking', 'Figuring', 'Sorting', 'Filtering',
    'Pulling', 'Focusing', 'Scoping', 'Mapping',
]


# -- Browser helpers --

def _cdp(page, method, params=None):
    try:
        s = page.context.new_cdp_session(page)
        result = s.send(method, params)
        s.detach()
        return result
    except Exception:
        return None


def clear_cache(page):
    _cdp(page, 'Network.clearBrowserCache')


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


# -- Pass 1: Fast RPC interception for text --

def intercept_rpc(page, chat_url: str) -> tuple[dict | None, int]:
    """Intercept RPC. Returns (parsed_data, content_length)."""
    clear_cache(page)
    content_length = 0
    
    # Capture response info via event
    response_info = {}
    
    def on_response(response):
        if 'ResolveDriveResource' in response.url:
            try:
                response_info['cl'] = int(response.headers.get('content-length', '0'))
            except:
                pass
    
    page.on('response', on_response)
    
    data = None
    try:
        with page.expect_response(
            lambda r: 'ResolveDriveResource' in r.url, timeout=45000
        ) as resp_info:
            page.goto(chat_url, wait_until="domcontentloaded", timeout=60000)
        data = resp_info.value.json()
        content_length = response_info.get('cl', len(json.dumps(data)))
    except Exception:
        pass
    
    if data is not None:
        return parse_rpc(data), content_length
    return None, 0


def parse_rpc(data) -> dict:
    """Parse RPC response into turns."""
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


# -- Pass 2: DOM attachment extraction (slow, only for large chats) --

def extract_attachments_dom(page, chat_dir: Path) -> list[dict]:
    """Scroll through every turn, extract images and document links from DOM."""
    attachments = []
    chat_dir.mkdir(parents=True, exist_ok=True)
    
    # Get turn count
    n = page.evaluate("""
        () => {
            const s = document.querySelector('.chat-session-content');
            return s ? s.querySelectorAll('.chat-turn-container').length : 0;
        }
    """)
    
    # Scroll to top
    page.evaluate("""
        const s = document.querySelector('.chat-session-content');
        if (s) s.scrollTo(0, 0);
    """)
    time.sleep(0.5)
    
    saved_imgs = 0
    found_docs = []
    
    for i in range(n):
        # Scroll this turn into view
        page.evaluate(f"""
            () => {{
                const s = document.querySelector('.chat-session-content');
                const turns = s?.querySelectorAll('.chat-turn-container');
                if (turns && turns[{i}]) turns[{i}].scrollIntoView({{block:'center',behavior:'instant'}});
            }}
        """)
        time.sleep(0.4)
        
        # Extract images from THIS turn
        imgs = page.evaluate(f"""
            () => {{
                const s = document.querySelector('.chat-session-content');
                const turns = s?.querySelectorAll('.chat-turn-container');
                const turn = turns && turns[{i}];
                if (!turn) return [];
                
                const results = [];
                const imgEls = turn.querySelectorAll('img');
                for (const img of imgEls) {{
                    const src = img.src || '';
                    if (src && !src.includes('google') && !src.includes('gstatic') &&
                        !src.includes('watermark') && !src.includes('material-symbols') &&
                        img.naturalWidth >= 50 && img.naturalHeight >= 50) {{
                        results.push({{
                            src: src,
                            alt: img.alt || '',
                            w: img.naturalWidth,
                            h: img.naturalHeight,
                        }});
                    }}
                }}
                return results;
            }}
        """)
        
        for j, img in enumerate(imgs):
            src = img['src']
            if src.startswith('data:'):
                try:
                    header, b64data = src.split(',', 1)
                    mime = header.split(':')[1].split(';')[0]
                    ext = mime.split('/')[1]
                    data = base64.b64decode(b64data)
                    filename = f"image_{saved_imgs + 1}.{ext}"
                    filepath = chat_dir / filename
                    # Avoid overwriting duplicates
                    if not filepath.exists():
                        with open(filepath, 'wb') as f:
                            f.write(data)
                        attachments.append({
                            'type': 'image',
                            'filename': filename,
                            'size': len(data),
                            'dimensions': f"{img['w']}x{img['h']}",
                            'alt': img.get('alt', ''),
                        })
                        saved_imgs += 1
                except Exception as e:
                    print(f"      Image save error: {e}")
            elif src.startswith('http'):
                # External image URL - just note it
                attachments.append({
                    'type': 'image_url',
                    'filename': '',
                    'url': src,
                    'dimensions': f"{img['w']}x{img['h']}",
                })
    
    # Extract document links from entire session
    docs = page.evaluate("""
        () => {
            const s = document.querySelector('.chat-session-content');
            if (!s) return [];
            const results = [];
            const anchors = s.querySelectorAll('a');
            for (const a of anchors) {
                const href = a.href || '';
                if (href && (href.includes('drive.google.com') ||
                             href.includes('docs.google.com') ||
                             href.endsWith('.pdf') || href.endsWith('.doc') ||
                             href.endsWith('.docx') || href.includes('/file/') ||
                             href.includes('/document/'))) {
                    // Find role
                    let turn = a.parentElement;
                    while (turn && !turn.className?.includes?.('chat-turn-container'))
                        turn = turn.parentElement;
                    const role = turn && turn.className.includes('user') ? 'user' : 'model';
                    results.push({href, role, text: (a.innerText||'').trim().substring(0, 100)});
                }
            }
            return results;
        }
    """)
    
    for doc in docs:
        attachments.append({
            'type': 'document_link',
            'url': doc['href'],
            'role': doc.get('role', 'unknown'),
            'description': doc.get('text', ''),
        })
    
    return attachments


# -- Library scraping --

def get_chat_list(page) -> list[dict]:
    """Scrape all chat links from the library."""
    print("Scanning library...")
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
    return unique


# -- Saving --

def slugify(title: str) -> str:
    return re.sub(r'[^\w\s-]', '', title)[:60].strip()


def save_chat(turns, attachments, chat_info, out_dir: Path) -> Path:
    """Save chat as markdown in its own folder with attachments."""
    title = chat_info.get('title', '')
    slug = slugify(title) or chat_info['id'][:20]
    chat_dir = out_dir / slug
    chat_dir.mkdir(parents=True, exist_ok=True)
    
    md_path = chat_dir / f"{slug}.md"
    counter = 1
    while md_path.exists():
        md_path = chat_dir / f"{slug}_{counter}.md"
        counter += 1
    
    ts = time.strftime('%Y-%m-%d %H:%M:%S')
    real = [t for t in turns if not t.get('is_thought')]
    
    imgs = [a for a in attachments if a['type'] == 'image']
    docs = [a for a in attachments if a['type'] == 'document_link']
    
    with open(md_path, 'w') as f:
        f.write(f"# {title}\n\n")
        f.write(f"**Source:** {chat_info['url']}\n\n")
        f.write(f"**Scraped:** {ts}\n\n")
        
        if imgs:
            f.write("## Attached Images\n\n")
            for a in imgs:
                f.write(f"- ![{a.get('alt', 'image')}]({a['filename']}) ({a.get('dimensions', '')}, {a.get('size', 0):,} bytes)\n")
            f.write("\n")
        if docs:
            f.write("## Attached Documents\n\n")
            for a in docs:
                f.write(f"- [{a.get('description', 'document')}]({a['url']})\n")
            f.write("\n")
        
        f.write("---\n\n")
        for t in real:
            label = {"user": "User", "model": "Model"}.get(t['role'], t['role'])
            f.write(f"## {label}\n\n{t['text']}\n\n---\n\n")
    
    return md_path


# -- Main --

def main():
    ap = argparse.ArgumentParser(description="Scrape Google AI Studio conversations")
    ap.add_argument("--url", help="Scrape a single chat URL")
    ap.add_argument("-o", "--output-dir", default=str(SCRIPT_DIR))
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
            if not chats:
                print("No chats found.")
                ctx.close()
                return

        print(f"\n{'=' * 50}")
        print(f"  {len(chats)} chats to scrape")
        print(f"{'=' * 50}\n")

        done = 0
        failed = 0
        total_imgs = 0
        total_docs = 0
        heavy_chats = 0

        for i, chat in enumerate(chats):
            try:
                label = chat['title'][:55] if chat['title'] else chat['id'][:20]
                
                # === PASS 1: Fast text extraction via API ===
                parsed, content_len = intercept_rpc(page, chat['url'])
                
                if parsed is None:
                    # API failed (likely huge response). Full DOM extraction.
                    print(f"[{i+1}/{len(chats)}] {label} [HEAVY - DOM only]")
                    clear_cache(page)
                    page.goto(chat['url'], wait_until="domcontentloaded", timeout=60000)
                    time.sleep(8)
                    
                    # Extract title
                    title = page.evaluate("""
                        () => {
                            const h1 = document.querySelector('h1');
                            if (h1) return h1.innerText.trim();
                            return '';
                        }
                    """) or chat.get('title', '')
                    
                    # Extract attachments (this scrolls through all turns)
                    slug = slugify(title) or chat['id'][:20]
                    attachments = extract_attachments_dom(page, out_dir / slug)
                    heavy_chats += 1
                    
                    # Extract text AFTER images (page was already scrolled)
                    turns_raw = page.evaluate("""
                        () => {
                            const s = document.querySelector('.chat-session-content');
                            if (!s) return [];
                            const containers = s.querySelectorAll('.chat-turn-container');
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
                                if (text && !seen.has(text.substring(0, 100))) {
                                    seen.add(text.substring(0, 100));
                                    // Detect thoughts
                                    let is_thought = false;
                                    if (role === 'model' && text.startsWith('**')) {
                                        const m = text.match(/^\\*\\*(.+?)\\*\\*/);
                                        if (m) {
                                            const first = m[1].split(' ')[0] || '';
                                            const indicators = ['Research','Search','Analyzing','Reviewing','Investigating','Scrutinizing','Checking','Examining','Refining','Evaluating','Formulating','Planning','Verifying','Compiling','Synthesizing','Gathering','Assessing','Pinpointing','Exploring','Defining','Comparing','Processing','Generating','Creating','Setting','Navigating','Opening','Extracting','Beginning','Starting','Preparing','Organizing','Summarizing','Translating','Calculating','Computing','Identifying','Locating','Finding','Retrieving','Comprehending','Understanding','Drafting','Outlining','Diving','Estimating','Crunching','Tackling','Working','Breaking','Deciding','Looking','Figuring','Sorting','Filtering','Pulling','Focusing','Scoping','Mapping'];
                                            if (indicators.some(w => first.startsWith(w))) is_thought = true;
                                        }
                                    }
                                    turns.push({role, text, len: text.length, is_thought});
                                }
                            }
                            return turns;
                        }
                    """)
                    parsed = {"title": title, "turns": turns_raw}
                else:
                    chat['title'] = parsed.get('title') or chat.get('title', '')
                    
                    # Check if we need Pass 2 (large response = likely has attachments)
                    if content_len > LARGE_RESPONSE_THRESHOLD:
                        print(f"[{i+1}/{len(chats)}] {label} [HEAVY - {content_len/1e6:.0f}MB]")
                        attachments = extract_attachments_dom(page, out_dir / slugify(chat['title']))
                        heavy_chats += 1
                    else:
                        attachments = []
                
                img_count = sum(1 for a in attachments if a['type'] == 'image')
                doc_count = sum(1 for a in attachments if a['type'] == 'document_link')
                total_imgs += img_count
                total_docs += doc_count
                
                real = [t for t in parsed.get('turns', []) if not t.get('is_thought')]
                u = sum(1 for t in real if t['role'] == 'user')
                m = sum(1 for t in real if t['role'] == 'model')
                chars = sum(t['len'] for t in real)
                
                extras = ""
                if img_count: extras += f", {img_count} img"
                if doc_count: extras += f", {doc_count} doc"
                
                md = save_chat(parsed.get('turns', []), attachments, chat, out_dir)
                print(f"  -> {len(real)}t ({u}u/{m}m), {chars:,} chars{extras}")
                print(f"     {md.relative_to(out_dir)}")
                done += 1
                
            except Exception as e:
                print(f"[{i+1}/{len(chats)}] {label} -> FAILED: {e}")
                failed += 1
            
            time.sleep(0.3)

        print(f"\n{'=' * 50}")
        print(f"  Done: {done} scraped, {failed} failed")
        print(f"  Heavy (with attachments): {heavy_chats}")
        print(f"  Images: {total_imgs}, Documents: {total_docs}")
        print(f"  Output: {out_dir}")
        print(f"{'=' * 50}")

        ctx.close()


if __name__ == "__main__":
    main()
