#!/usr/bin/env python3.14
"""
Scrape Google AI Studio conversations — text, images, and documents.

Two-pass approach:
  Pass 1 (fast): Intercept RPC for text.
  Pass 2 (if images detected OR RPC failed): Scroll DOM for text + screenshot images.

Each chat saved in its own folder: <slug>/<slug>.md + image_*.png

USAGE:
  python3.14 scrape_aistudio.py              # Full library
  python3.14 scrape_aistudio.py --url <URL>  # Single chat
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
LIBRARY_URL = "https://aistudio.google.com/library"
SCRIPT_DIR = Path(__file__).resolve().parent
USER_DATA_DIR = str(SCRIPT_DIR / ".playwright-profile")

THOUGHT_INDICATORS = [
    # All model thoughts start with a bold gerund/verb heading.
    # We match if the bold word starts with any of these stems.
    'Research', 'Search', 'Analyz', 'Review', 'Investigat',
    'Scrutiniz', 'Check', 'Examin', 'Refin', 'Evaluat',
    'Formulat', 'Plan', 'Verif', 'Compil', 'Synthes',
    'Gather', 'Assess', 'Pinpoint', 'Explor', 'Defin',
    'Compar', 'Process', 'Generat', 'Creat', 'Sett',
    'Navigat', 'Open', 'Extract', 'Beginn', 'Begin', 'Start',
    'Prepar', 'Organiz', 'Summariz', 'Translat', 'Calculat',
    'Comput', 'Identif', 'Locat', 'Find', 'Retriev',
    'Comprehend', 'Understand', 'Draft', 'Outlin', 'Div',
    'Estimat', 'Crunche', 'Tackl', 'Work', 'Break',
    'Decid', 'Look', 'Figur', 'Sort', 'Filter',
    'Pull', 'Focus', 'Scop', 'Map', 'Hon', 'Brainstorm',
    'Deconstruct', 'Construct', 'Polish', 'Detail',
    'Decipher', 'Interpret', 'Consider', 'Initiat', 'Clarif',
    'Unpack', 'Establish', 'Address', 'Fram', 'Determin',
    'Connect', 'Unravel', 'Dissect', 'Decompos', 'Encapsul',
    'Elucidat', 'Re-evaluat', 'Reexamin', 'Reconsider',
    'Synthesiz', 'Consolidat', 'Integrat', 'Compil',
    'Diagnos', 'Troubleshoot', 'Debug', 'Optimiz',
    'Visualiz', 'Conceptualiz', 'Structur', 'Architect',
    'Reconcil', 'Harmoniz', 'Align', 'Calibrat',
    'Propos', 'Recommend', 'Suggest', 'Advise',
]

# Model turns starting with self-referential introspection are thoughts.
INTROSPECTION_PREFIXES = [
    "I've been", "I'm currently", "I'm going", "I'm working", "I'm thinking",
    "I am currently", "I am going", "I am working", "I am thinking",
    "I need to", "I'll need to", "I will need to", "I'll start", "I will start",
    "I'll focus", "I will focus", "I'll begin", "I will begin",
    "Let me", "Let\u2019s", "Let's", "Let us", "My goal", "My aim", "My task",
    "First, I", "First I", "Now I", "Next, I", "Next I",
    "The user is", "The user wants", "The user's", "The query",
]


# -- Browser helpers --
def clear_cache(page):
    try:
        s = page.context.new_cdp_session(page)
        s.send('Network.clearBrowserCache')
        s.detach()
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


# -- RPC interception (Pass 1) --
def intercept_rpc(page, chat_url: str) -> tuple[dict | None, int]:
    """Intercept ResolveDriveResource RPC. Returns (parsed_data, content_length)."""
    clear_cache(page)
    rinfo = {}
    def _on_response(response):
        if 'ResolveDriveResource' in response.url:
            try:
                rinfo['cl'] = int(response.headers.get('content-length', '0'))
            except Exception:
                pass
    page.on('response', _on_response)
    try:
        with page.expect_response(lambda r: 'ResolveDriveResource' in r.url, timeout=45000) as resp_info:
            page.goto(chat_url, wait_until="domcontentloaded", timeout=60000)
        return parse_rpc(resp_info.value.json()), rinfo.get('cl', 0)
    except Exception:
        return None, rinfo.get('cl', 0)
    finally:
        page.remove_listener('response', _on_response)

def parse_rpc(data) -> dict:
    try:
        inner = data[0]
        title = inner[4][0] if len(inner) > 4 and inner[4] and inner[4][0] else ""
        turns = []
        for group in inner[13] if len(inner) > 13 else []:
            group_turns = []
            for turn in group:
                if not isinstance(turn, list) or len(turn) < 9:
                    continue
                text = turn[0] if isinstance(turn[0], str) else ""
                role = turn[8] if isinstance(turn[8], str) else "unknown"
                if not text.strip():
                    continue
                group_turns.append({"role": role, "text": text})
            
            # All non-last model turns in a sequence are thoughts.
            # The LAST model turn before a user turn (or end) is the actual answer.
            model_indices = [i for i, t in enumerate(group_turns) if t['role'] == 'model']
            for i, t in enumerate(group_turns):
                is_thought = False
                if t['role'] == 'model':
                    # It's a thought if there's another model turn after it before the next user
                    next_model = next((j for j in model_indices if j > i), None)
                    if next_model is not None:
                        # Check that there's no user turn between them
                        between = group_turns[i+1:next_model]
                        if not any(b['role'] == 'user' for b in between):
                            is_thought = True
                turns.append({"role": t['role'], "text": t['text'],
                              "len": len(t['text']), "is_thought": is_thought})
        return {"title": title, "turns": turns}
    except Exception as e:
        return {"title": "", "turns": [], "error": str(e)}


# -- DOM extraction (Pass 2: text + images + doc links) --
def extract_all_from_dom(page, chat_dir: Path) -> tuple[list[dict], list[dict]]:
    """Scroll through every turn: extract text AND screenshot ms-image-chunk elements."""
    turns = []
    attachments = []
    seen_texts = set()
    chat_dir.mkdir(parents=True, exist_ok=True)

    n = page.evaluate("""() => {
        const s = document.querySelector('.chat-session-content');
        return s ? s.querySelectorAll('.chat-turn-container').length : 0;
    }""")
    page.evaluate("""() => {
        const s = document.querySelector('.chat-session-content');
        if (s) s.scrollTo(0, 0);
    }""")
    time.sleep(0.3)

    saved_imgs = 0
    indicators_js = json.dumps(THOUGHT_INDICATORS)
    introspection_js = json.dumps(INTROSPECTION_PREFIXES)

    for i in range(n):
        # Scroll turn into view
        page.evaluate(f"""(i) => {{
            const s = document.querySelector('.chat-session-content');
            const turns = s?.querySelectorAll('.chat-turn-container');
            if (turns && turns[i]) turns[i].scrollIntoView({{block:'center',behavior:'instant'}});
        }}""", i)
        time.sleep(0.35)

        # ---- Screenshot image if present ----
        has_img = page.evaluate(f"""(i) => {{
            const turns = document.querySelectorAll('.chat-turn-container');
            return turns[i]?.querySelector('ms-image-chunk') !== null;
        }}""", i)
        if has_img:
            try:
                page.wait_for_function(f"""(i) => {{
                    const turns = document.querySelectorAll('.chat-turn-container');
                    const t = turns[i];
                    if (!t) return true;
                    const img = t.querySelector('ms-image-chunk img, img');
                    return !img || (img.complete && img.naturalWidth > 50);
                }}""", i, timeout=15000)
            except Exception:
                pass
            handle = page.evaluate_handle(f"""(i) => {{
                const turns = document.querySelectorAll('.chat-turn-container');
                return turns[i]?.querySelector('ms-image-chunk');
            }}""", i)
            if handle:
                try:
                    saved_imgs += 1
                    filename = f"image_{saved_imgs}.png"
                    filepath = chat_dir / filename
                    handle.screenshot(path=str(filepath))
                    attachments.append({
                        'type': 'image', 'filename': filename,
                        'size': filepath.stat().st_size,
                    })
                except Exception as e:
                    print(f"      Image error: {e}")
                handle.dispose()

        # ---- Extract text ----
        result = page.evaluate(f"""(i) => {{
            const indicators = {indicators_js};
            const introPrefixes = {introspection_js};
            const turns = document.querySelectorAll('.chat-turn-container');
            const t = turns[i];
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
            if (role === 'model') {{
                // Check for **BoldThought** pattern
                if (text.startsWith('**')) {{
                    const m = text.match(/^\\*\\*(.+?)\\*\\*/);
                    if (m) {{
                        const first = (m[1].split(' ')[0] || '');
                        if (indicators.some(w => first.startsWith(w))) is_thought = true;
                    }}
                }}
                // Also check for non-bold introspection (after optional **Bold** header)
                if (!is_thought) {{
                    const body = text.replace(/^\\*\\*[\\s\\S]+?\\*\\*/, '').trimLeft();
                    if (introPrefixes.some(p => body.startsWith(p))) is_thought = true;
                }}
            }}
            return {{text, role, is_thought}};
        }}""", i)

        if result.get('text') and len(result['text']) > 10:
            key = result['text'][:100]
            if key not in seen_texts:
                seen_texts.add(key)
                turns.append({
                    'role': result['role'], 'text': result['text'],
                    'len': len(result['text']), 'is_thought': result['is_thought'],
                })

    # Post-process: apply positional thought detection.
    # All non-last model turns in a sequence between user turns are thoughts.
    model_indices = [i for i, t in enumerate(turns) if t['role'] == 'model']
    for i, t in enumerate(turns):
        if t['role'] == 'model':
            next_model = next((j for j in model_indices if j > i), None)
            if next_model is not None:
                between = turns[i+1:next_model]
                if not any(b['role'] == 'user' for b in between):
                    t['is_thought'] = True
                else:
                    t['is_thought'] = False
            else:
                t['is_thought'] = False

    # ---- Document links ----
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


def page_has_images(page) -> bool:
    try:
        n = page.evaluate("""() => {
            const s = document.querySelector('.chat-session-content');
            return s ? s.querySelectorAll('ms-image-chunk').length : 0;
        }""")
        return n > 0
    except Exception:
        return False


# -- Library --
def get_chat_list(page) -> list[dict]:
    print("Scanning library...")
    for _ in range(35):
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        time.sleep(1.2)
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
            label = (chat['title'] or chat['id'])[:55]
            try:
                # Pass 1: fast RPC for text
                parsed, _ = intercept_rpc(page, chat['url'])

                # If RPC failed, full DOM fallback
                if parsed is None:
                    print(f"[{i+1}/{len(chats)}] {label} [DOM fallback]")
                    clear_cache(page)
                    page.goto(chat['url'], wait_until="domcontentloaded", timeout=60000)
                    try:
                        page.wait_for_selector('.chat-session-content .chat-turn-container', timeout=30000)
                    except Exception:
                        pass
                    time.sleep(4)
                    title = page.evaluate("""() => {
                        const h1 = document.querySelector('h1');
                        return h1 ? h1.innerText.trim() : '';
                    }""") or chat.get('title', '')
                    slug = slugify(title) or chat['id'][:20]
                    turns, attachments = extract_all_from_dom(page, out_dir / slug)
                    parsed = {"title": title, "turns": turns}
                    heavy += 1
                else:
                    # RPC succeeded — but check for images
                    chat['title'] = parsed.get('title') or chat.get('title', '')
                    if page_has_images(page):
                        _, attachments = extract_all_from_dom(page, out_dir / slugify(chat['title']))
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
                save_chat(parsed.get('turns', []), attachments, chat, out_dir)
                print(f"  -> {len(real)}t ({u}u/{m}m), {chars:,} chars{extra}")
                done += 1
            except Exception as e:
                print(f"  -> FAILED: {e}")
                traceback.print_exc()
                failed += 1
            time.sleep(0.2)

        print(f"\n{'='*50}")
        print(f"  Done: {done} ok, {failed} failed, {heavy} heavy")
        print(f"  Images: {imgs_total}, Documents: {docs_total}")
        print(f"  Output: {out_dir}")
        print(f"{'='*50}")
        ctx.close()

if __name__ == "__main__":
    main()
