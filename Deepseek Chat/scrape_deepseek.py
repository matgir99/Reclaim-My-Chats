#!/usr/bin/env python3.14
"""
Scrape DeepSeek Chat conversations — text, thinking, and images.

Pure DOM-based extraction. Each chat is a SPA; messages load into the DOM
with class-based markers for user vs assistant turns.

Each chat saved in its own folder: <slug>/<slug>.md

USAGE:
  python3.14 scrape_deepseek.py              # Full library
  python3.14 scrape_deepseek.py --url <URL>  # Single chat
"""

import argparse
import json
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

# DeepSeek-specific DOM selectors
MESSAGE_SEL = '.ds-message'
USER_MESSAGE_CLASS = 'd29f3d7d'    # user messages have this class
USER_TEXT_SEL = '.fbb737a4'         # user text inside message
THINKING_SEL = '._74c0879'           # model reasoning
ANSWER_SEL = '.ds-markdown.ds-assistant-message-main-content'  # final answer


# -- Browser --
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
                """() => document.querySelector('.ds-message, textarea) !== null""",
                timeout=300000
            )
            print("Logged in!\n")
            time.sleep(3)
        except PwTimeout:
            raise RuntimeError("Login timeout")


# -- Chat list --
def get_chat_list(page) -> list[dict]:
    print("Scanning sidebar...")
    page.goto(BASE_URL, wait_until="domcontentloaded", timeout=60000)
    time.sleep(3)
    
    # Scroll the sidebar to load all chats
    for _ in range(20):
        page.evaluate("""() => {
            const sidebar = document.querySelector('[class*="sidebar"]');
            if (sidebar) sidebar.scrollTo(0, sidebar.scrollHeight);
        }""")
        time.sleep(0.8)
    
    chats = page.evaluate("""() => {
        const R = [], S = new Set();
        document.querySelectorAll('a[href*="/a/chat/s/"]').forEach(a => {
            const href = a.href || '';
            const id = href.split('/a/chat/s/')[1]?.split('?')[0];
            if (id && !S.has(id)) {
                S.add(id);
                R.push({
                    id,
                    url: href.split('?')[0],
                    title: (a.innerText || '').trim().substring(0, 200) || '(no title)',
                });
            }
        });
        return R;
    }""")
    return chats


# -- Extraction --
def extract_messages(page) -> list[dict]:
    """Extract all messages currently in the DOM."""
    messages = page.evaluate("""() => {
        const msgEls = document.querySelectorAll('.ds-message');
        const results = [];
        
        msgEls.forEach(msg => {
            const cls = (msg.className || '').toString();
            const isUser = cls.includes('d29f3d7d');
            
            if (isUser) {
                const textEl = msg.querySelector('.fbb737a4');
                const text = (textEl?.innerText || '').trim();
                if (text) {
                    results.push({role: 'user', text, thinking: '', turnIndex: results.length});
                }
            } else {
                const thinkingEl = msg.querySelector('._74c0879');
                const answerEl = msg.querySelector('.ds-markdown.ds-assistant-message-main-content');
                const thinking = (thinkingEl?.innerText || '').trim();
                const answer = (answerEl?.innerText || '').trim();
                if (thinking || answer) {
                    results.push({
                        role: 'assistant',
                        text: answer,
                        thinking: thinking,
                        turnIndex: results.length,
                    });
                }
            }
        });
        
        return results;
    }""")
    return messages


def load_all_messages(page) -> int:
    """Scroll through entire chat to trigger virtual scroller to load all messages.
    Returns number of messages found."""
    prev_count = 0
    for attempt in range(60):
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        time.sleep(0.5)
        current = page.evaluate("""() => document.querySelectorAll('.ds-message').length""")
        if current == prev_count and attempt > 5:
            break
        prev_count = current
    return current


def extract_images(page, chat_dir: Path) -> list[dict]:
    """Screenshot images found in assistant messages."""
    attachments = []
    images = page.evaluate("""() => {
        const results = [];
        // Find images inside assistant messages (not in sidebar)
        document.querySelectorAll('.ds-markdown img').forEach((img, i) => {
            if (img.naturalWidth > 50) {
                results.push({index: i, w: img.naturalWidth, h: img.naturalHeight});
            }
        });
        return results;
    }""")
    
    if not images:
        return attachments
    
    chat_dir.mkdir(parents=True, exist_ok=True)
    img_els = page.query_selector_all('.ds-markdown img')
    
    saved = 0
    for i, el in enumerate(img_els):
        try:
            if el.get_attribute('naturalWidth') == '0':
                continue
            saved += 1
            filename = f"image_{saved}.png"
            filepath = chat_dir / filename
            el.screenshot(path=str(filepath))
            attachments.append({
                'type': 'image',
                'filename': filename,
                'size': filepath.stat().st_size,
            })
        except Exception:
            pass
    
    return attachments


# -- Saving --
def slugify(title: str) -> str:
    return re.sub(r'[^\w\s-]', '', title)[:60].strip()

def save_chat(messages, attachments, chat_info, out_dir: Path) -> Path:
    title = chat_info.get('title', '')
    slug = slugify(title) or chat_info['id'][:20]
    chat_dir = out_dir / slug
    chat_dir.mkdir(parents=True, exist_ok=True)
    md = chat_dir / f"{slug}.md"
    c = 1
    while md.exists():
        md = chat_dir / f"{slug}_{c}.md"; c += 1
    ts = time.strftime('%Y-%m-%d %H:%M:%S')
    imgs = [a for a in attachments if a['type'] == 'image']
    
    with open(md, 'w') as f:
        f.write(f"# {title}\n\n**Source:** {chat_info['url']}\n\n**Scraped:** {ts}\n\n")
        if imgs:
            f.write("## Images\n\n")
            for a in imgs:
                f.write(f"- ![{a.get('alt', 'image')}]({a['filename']})\n")
            f.write("\n")
        f.write("---\n\n")
        
        for t in messages:
            if t['role'] == 'user':
                f.write(f"## User\n\n{t['text']}\n\n---\n\n")
            else:
                if t.get('thinking'):
                    f.write(f"<details>\n<summary>Thinking</summary>\n\n{t['thinking']}\n\n</details>\n\n")
                if t.get('text'):
                    f.write(f"## Assistant\n\n{t['text']}\n\n---\n\n")
    return md


# -- Main --
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", help="Scrape single chat URL")
    ap.add_argument("-o", "--output-dir", default=str(SCRIPT_DIR))
    ap.add_argument("--include-thinking", action="store_true",
                    help="Include model thinking in output (default: hidden in <details>)")
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
        done = failed = imgs_total = 0

        for i, chat in enumerate(chats):
            label = (chat['title'] or chat['id'])[:60]
            try:
                page.goto(chat['url'], wait_until="domcontentloaded", timeout=60000)
                time.sleep(3)
                
                # Get title from page if missing
                if not chat.get('title'):
                    chat['title'] = page.evaluate("""() => {
                        const t = document.title || '';
                        const dash = t.lastIndexOf(' - DeepSeek');
                        return dash > 0 ? t.substring(0, dash).trim() : t;
                    }""") or chat['id'][:20]
                
                # Load all messages via scrolling
                msg_count = load_all_messages(page)
                
                # Extract
                messages = extract_messages(page)
                attachments = extract_images(page, out_dir / slugify(chat['title'] or chat['id']))
                
                imgs = len(attachments)
                imgs_total += imgs
                users = sum(1 for m in messages if m['role'] == 'user')
                assistants = sum(1 for m in messages if m['role'] == 'assistant')
                chars = sum(len(m['text']) for m in messages)
                extra = f", {imgs} img" if imgs else ""
                
                save_chat(messages, attachments, chat, out_dir)
                print(f"[{i+1}/{len(chats)}] {label}")
                print(f"  -> {len(messages)}t ({users}u/{assistants}a), {chars:,} chars{extra}")
                done += 1
            except Exception as e:
                print(f"  -> FAILED: {e}")
                traceback.print_exc()
                failed += 1
            time.sleep(0.2)

        print(f"\n{'='*50}")
        print(f"  Done: {done} ok, {failed} failed")
        print(f"  Images: {imgs_total}")
        print(f"  Output: {out_dir}")
        print(f"{'='*50}")
        ctx.close()

if __name__ == "__main__":
    main()
