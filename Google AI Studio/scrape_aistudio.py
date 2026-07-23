#!/usr/bin/env python3.14
"""
Scrape Google AI Studio conversations — text, images, and documents.

Two-pass approach:
  Pass 1 (fast): Intercept RPC for text. Check content-length header.
  Pass 2 (slow, only for heavy chats): Scroll DOM to screenshot images.

Each chat saved in its own folder: <title>/<title>.md + image_*.png

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
LARGE_RESPONSE_THRESHOLD = 1_000_000  # 1 MB -> do DOM pass

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
        r = s.send(method, params)
        s.detach()
        return r
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


# -- RPC interception (Pass 1) --
def intercept_rpc(page, chat_url: str) -> tuple[dict | None, int]:
    clear_cache(page)
    response_info = {}
    def on_response(response):
        if 'ResolveDriveResource' in response.url:
            try:
                response_info['cl'] = int(response.headers.get('content-length', '0'))
            except Exception:
                pass
    page.on('response', on_response)
    data = None
    try:
        with page.expect_response(lambda r: 'ResolveDriveResource' in r.url, timeout=45000) as resp_info:
            page.goto(chat_url, wait_until="domcontentloaded", timeout=60000)
        data = resp_info.value.json()
    except Exception:
        pass
    return parse_rpc(data) if data else None, response_info.get('cl', 0)

def parse_rpc(data) -> dict:
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


# -- DOM extraction (Pass 2, heavy chats only) --
def extract_from_dom(page, chat_dir: Path) -> tuple[list[dict], list[dict]]:
    """Scroll every turn, extract text + screenshot ms-image-chunk elements."""
    attachments = []
    turns = []
    seen_texts = set()
    chat_dir.mkdir(parents=True, exist_ok=True)

    n = page.evaluate("""
        () => { const s = document.querySelector('.chat-session-content');
                return s ? s.querySelectorAll('.chat-turn-container').length : 0; }
    """)
    page.evaluate("""
        () => { const s = document.querySelector('.chat-session-content');
                if (s) s.scrollTo(0, 0); }
    """)
    time.sleep(0.3)

    saved_imgs = 0
    indicators_js = json.dumps(THOUGHT_INDICATORS)

    for i in range(n):
        # Scroll turn into view
        page.evaluate(f"""() => {{
            const s = document.querySelector('.chat-session-content');
            const turns = s?.querySelectorAll('.chat-turn-container');
            if (turns && turns[{i}]) turns[{i}].scrollIntoView({{block:'center',behavior:'instant'}});
        }}""")
        time.sleep(0.35)

        # If this turn has an image chunk, screenshot it
        has_img = page.evaluate(f"""() => {{
            const turns = document.querySelectorAll('.chat-turn-container');
            return turns[{i}]?.querySelector('ms-image-chunk') !== null;
        }}""")
        if has_img:
            try:
                page.wait_for_function(f"""() => {{
                    const turns = document.querySelectorAll('.chat-turn-container');
                    const t = turns[{i}];
                    if (!t) return true;
                    const img = t.querySelector('ms-image-chunk img, img');
                    return !img || (img.complete && img.naturalWidth > 50);
                }}""", timeout=15000)
            except Exception:
                pass
            handle = page.evaluate_handle(f"""() => {{
                const turns = document.querySelectorAll('.chat-turn-container');
                return turns[{i}]?.querySelector('ms-image-chunk');
            }}""")
            if handle:
                try:
                    filename = f"image_{saved_imgs + 1}.png"
                    filepath = chat_dir / filename
                    handle.screenshot(path=str(filepath))
                    attachments.append({
                        'type': 'image', 'filename': filename,
                        'size': filepath.stat().st_size,
                    })
                    saved_imgs += 1
                except Exception as e:
                    print(f"      Image error: {e}")
                handle.dispose()

        # Extract text
        result = page.evaluate(f"""() => {{
            const indicators = {indicators_js};
            const turns = document.querySelectorAll('.chat-turn-container');
            const t = turns[{i}];
            if (!t) return {{text:'',role:'unknown',is_thought:false}};
            const cls = (t.className || '').toString().toLowerCase();
            let role = cls.includes(' user') ? 'user' : (cls.includes(' model') ? 'model' : 'unknown');
            const chunks = t.querySelectorAll('ms-text-chunk');
            const parts = [];
            for (const c of chunks) {{
                const txt = (c.textContent || '').trim();
                if (txt && txt.length > 5) parts.push(txt);
            }}
            let text = parts.join('\\n\\n');
            let is_thought = false;
            if (role === 'model' && text.startsWith('**')) {{
                const m = text.match(/^\\*\\*(.+?)\\*\\*/);
                if (m) {{
                    const first = m[1].split(' ')[0] || '';
                    if (indicators.some(w => first.startsWith(w))) is_thought = true;
                }}
            }}
            return {{text, role, is_thought}};
        }}""")

        if result.get('text') and len(result['text']) > 10:
            key = result['text'][:100]
            if key not in seen_texts:
                seen_texts.add(key)
                turns.append({
                    'role': result['role'], 'text': result['text'],
                    'len': len(result['text']), 'is_thought': result['is_thought'],
                })

    # Document links
    docs = page.evaluate("""() => {
        const s = document.querySelector('.chat-session-content');
        if (!s) return [];
        const r = [];
        s.querySelectorAll('a').forEach(a => {
            const h = a.href || '';
            if (h && (h.includes('drive.google.com') || h.includes('docs.google.com') ||
                     h.endsWith('.pdf') || h.endsWith('.doc') || h.endsWith('.docx') ||
                     h.includes('/file/') || h.includes('/document/')))
                r.push({href: h, text: (a.innerText||'').trim().substring(0, 100)});
        });
        return r;
    }""")
    for doc in docs:
        attachments.append({'type': 'document_link', 'url': doc['href'], 'description': doc['text']})

    return turns, attachments


# -- Library scraping --
def get_chat_list(page) -> list[dict]:
    print("Scanning library...")
    for _ in range(30):
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        time.sleep(1.5)
    links = page.evaluate("""() => {
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
    }""")
    seen = set()
    return [l for l in links if not (l['url'] in seen or seen.add(l['url']))]


# -- Saving --
def slugify(title: str) -> str:
    return re.sub(r'[^\w\s-]', '', title)[:60].strip()

def save_chat(turns, attachments, chat_info, out_dir: Path) -> Path:
    title = chat_info.get('title', '')
    slug = slugify(title) or chat_info['id'][:20]
    chat_dir = out_dir / slug
    chat_dir.mkdir(parents=True, exist_ok=True)
    md = chat_dir / f"{slug}.md"
    c = 1
    while md.exists():
        md = chat_dir / f"{slug}_{c}.md"; c += 1
    ts = time.strftime('%Y-%m-%d %H:%M:%S')
    real = [t for t in turns if not t.get('is_thought')]
    imgs = [a for a in attachments if a['type'] == 'image']
    docs = [a for a in attachments if a['type'] == 'document_link']
    with open(md, 'w') as f:
        f.write(f"# {title}\n\n**Source:** {chat_info['url']}\n\n**Scraped:** {ts}\n\n")
        if imgs:
            f.write("## Images\n\n")
            for a in imgs:
                f.write(f"- ![{a.get('alt', 'image')}]({a['filename']})\n")
            f.write("\n")
        if docs:
            f.write("## Documents\n\n")
            for a in docs:
                f.write(f"- [{a.get('description', 'doc')}]({a['url']})\n")
            f.write("\n")
        f.write("---\n\n")
        for t in real:
            label = {"user": "User", "model": "Model"}.get(t['role'], t['role'])
            f.write(f"## {label}\n\n{t['text']}\n\n---\n\n")
    return md


# -- Main --
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", help="Scrape single chat URL")
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
                  "--disable-features=AutomationControlled", "--disable-dev-shm-usage"],
            viewport={"width": 1400, "height": 900},
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        ensure_logged_in(page)

        if args.url:
            chats = [{'id': args.url.split('/')[-1][:40], 'url': args.url, 'title': ''}]
        else:
            chats = get_chat_list(page)
        if not chats:
            print("No chats."); ctx.close(); return

        print(f"\n{'='*50}\n  {len(chats)} chats\n{'='*50}\n")
        done = failed = heavy = imgs_total = docs_total = 0

        for i, chat in enumerate(chats):
            try:
                label = (chat['title'] or chat['id'])[:55]
                parsed, content_len = intercept_rpc(page, chat['url'])

                if parsed is None:
                    print(f"[{i+1}/{len(chats)}] {label} [DOM fallback]")
                    clear_cache(page)
                    page.goto(chat['url'], wait_until="domcontentloaded", timeout=60000)
                    try:
                        page.wait_for_selector('.chat-session-content .chat-turn-container', timeout=30000)
                    except Exception:
                        pass
                    time.sleep(3)
                    title = page.evaluate("""() => {
                        const h1 = document.querySelector('h1');
                        return h1 ? h1.innerText.trim() : '';
                    }""") or chat.get('title', '')
                    slug = slugify(title) or chat['id'][:20]
                    turns, attachments = extract_from_dom(page, out_dir / slug)
                    parsed = {"title": title, "turns": turns}
                    heavy += 1
                else:
                    chat['title'] = parsed.get('title') or chat.get('title', '')
                    if content_len > LARGE_RESPONSE_THRESHOLD:
                        print(f"[{i+1}/{len(chats)}] {label} [heavy {content_len/1e6:.0f}MB]")
                        _, attachments = extract_from_dom(page, out_dir / slugify(chat['title']))
                        heavy += 1
                    else:
                        attachments = []

                imgs = sum(1 for a in attachments if a['type'] == 'image')
                docs = sum(1 for a in attachments if a['type'] == 'document_link')
                imgs_total += imgs; docs_total += docs
                real = [t for t in parsed.get('turns', []) if not t.get('is_thought')]
                u = sum(1 for t in real if t['role'] == 'user')
                m = sum(1 for t in real if t['role'] == 'model')
                chars = sum(t['len'] for t in real)
                extra = f", {imgs} img" if imgs else ""
                extra += f", {docs} doc" if docs else ""
                md = save_chat(parsed.get('turns', []), attachments, chat, out_dir)
                print(f"  -> {len(real)}t ({u}u/{m}m), {chars:,} chars{extra}")
                done += 1
            except Exception as e:
                print(f"  -> FAILED: {e}")
                failed += 1
            time.sleep(0.3)

        print(f"\n{'='*50}")
        print(f"  Done: {done} ok, {failed} failed, {heavy} heavy")
        print(f"  Images: {imgs_total}, Documents: {docs_total}")
        print(f"  Output: {out_dir}")
        print(f"{'='*50}")
        ctx.close()

if __name__ == "__main__":
    main()
