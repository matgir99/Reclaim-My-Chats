#!/usr/bin/env python3.14
"""
Scrape Google AI Studio conversations — replay-first architecture.

Instead of navigating to each chat and hoping the app fires (and lets us read)
the ResolveDriveResource RPC, we REPLAY that RPC ourselves from page context
(SAPISIDHASH auth, exactly like the app does). This eliminates the two historic
failure modes:
  * Chrome evicting huge RPC bodies from the inspector cache (e.g. a 116 MB
    response for an image-heavy chat),
  * the app serving chats from its own in-memory cache without firing the RPC.

Data recovered per chat (all from the RPC JSON):
  * text turns with role + thought flag (thought = turn[19] truthy)
  * inline images (base64 at turn[12]) saved at ORIGINAL quality
  * user-uploaded Drive attachments (file IDs at turn[1]) downloaded via
    drive/v3 alt=media with a replayed GenerateAccessToken Bearer token

Fallback (replay fails): legacy response interception, then DOM scraping.

USAGE:
  python3.14 scrape_googleaistudio.py                 # full library
  python3.14 scrape_googleaistudio.py --url <URL>     # single chat
  python3.14 scrape_googleaistudio.py --only qnap     # title substring filter
  python3.14 scrape_googleaistudio.py --start 10 --limit 5
  python3.14 scrape_googleaistudio.py --resume        # skip already-saved chats
  python3.14 scrape_googleaistudio.py --keep-raw      # also save raw RPC JSON
"""

import argparse
import base64
import json
import os
import re
import sys
import time
import traceback
from html.parser import HTMLParser
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout
except ImportError:
    print("ERROR: playwright needed.")
    sys.exit(1)

# -- Config --
LIBRARY_URL = "https://aistudio.google.com/library"
SCRIPT_DIR = Path(__file__).resolve().parent
USER_DATA_DIR = str(SCRIPT_DIR.parent / ".playwright-profile")
RPC_BASE = ("https://alkalimakersuite-pa.clients6.google.com/$rpc/"
            "google.internal.alkali.applications.makersuite.v1.MakerSuiteService/")
DRIVE_API = "https://www.googleapis.com/drive/v3/files"


# ---------------------------------------------------------------------------
# In-page RPC replay helper (SAPISIDHASH auth, like the app itself)
# ---------------------------------------------------------------------------
RPC_JS = r"""
window.__msApiKey = window.__msApiKey || null;
window.__msRpc = async function(rpcName, payload) {
    const getCookie = (n) => {
        const m = document.cookie.match(new RegExp('(?:^|; )' + n + '=([^;]*)'));
        return m ? decodeURIComponent(m[1]) : null;
    };
    const sapisid = getCookie('SAPISID') || getCookie('__Secure-3PAPISID');
    if (!sapisid) throw new Error('no SAPISID cookie');
    const origin = 'https://aistudio.google.com';
    const ts = Math.floor(Date.now() / 1000);
    const digest = await crypto.subtle.digest('SHA-1',
        new TextEncoder().encode(`${ts} ${sapisid} ${origin}`));
    const hex = [...new Uint8Array(digest)].map(b => b.toString(16).padStart(2, '0')).join('');
    const headers = {
        'content-type': 'application/json+protobuf',
        'authorization': `SAPISIDHASH ${ts}_${hex}`,
        'x-user-agent': 'grpc-web-javascript/0.1',
        'referer': origin + '/',
    };
    if (window.__msApiKey) headers['x-goog-api-key'] = window.__msApiKey;
    const url = 'https://alkalimakersuite-pa.clients6.google.com/$rpc/' +
        'google.internal.alkali.applications.makersuite.v1.MakerSuiteService/' + rpcName;
    const resp = await fetch(url, {
        method: 'POST', credentials: 'include',
        headers, body: JSON.stringify(payload),
    });
    const text = await resp.text();
    return {status: resp.status, len: text.length, text};
};
'ready'
"""


def init_replay(page):
    """Inject the RPC helper and capture the app's public API key (memory only)."""
    def grab_key(req):
        if 'alkalimakersuite-pa.clients6.google.com' in req.url:
            k = req.headers.get('x-goog-api-key')
            if k:
                try:
                    page.evaluate("(k) => { window.__msApiKey = k; }", k)
                except Exception:
                    pass
    page.on('request', grab_key)
    page.goto(LIBRARY_URL, wait_until="domcontentloaded", timeout=60000)
    time.sleep(2)
    page.evaluate(RPC_JS)
    time.sleep(4)
    page.remove_listener('request', grab_key)


def rpc_call(page, name: str, payload: list, retries: int = 3) -> tuple[int, str]:
    """Call a MakerSuite RPC via in-page fetch. Returns (status, text)."""
    last_err = None
    for attempt in range(retries):
        try:
            r = page.evaluate("(a) => window.__msRpc(a.n, a.p)",
                              {'n': name, 'p': payload})
            if r['status'] == 200:
                return r['status'], r['text']
            if r['status'] in (401, 403) and attempt < retries - 1:
                time.sleep(2)  # let cookies refresh
                continue
            return r['status'], r['text']
        except Exception as e:
            last_err = e
            time.sleep(2 + attempt * 2)
    raise RuntimeError(f'RPC {name} failed after {retries} attempts: {last_err}')


# ---------------------------------------------------------------------------
# Drive attachment downloads (Bearer token from replayed GenerateAccessToken)
# ---------------------------------------------------------------------------
class DriveClient:
    def __init__(self, page):
        self.page = page
        self._token = None
        self._token_ts = 0

    def token(self) -> str:
        # Refresh every 30 min or on demand
        if self._token and (time.time() - self._token_ts) < 1800:
            return self._token
        status, text = rpc_call(self.page, 'GenerateAccessToken', ['users/me'])
        if status != 200:
            raise RuntimeError(f'GenerateAccessToken HTTP {status}')
        token = self._find_token(json.loads(text))
        if not token:
            raise RuntimeError('no access token in GenerateAccessToken response')
        self._token = token
        self._token_ts = time.time()
        return token

    @staticmethod
    def _find_token(o):
        if isinstance(o, str) and o.startswith('ya29.') and 50 < len(o) < 600:
            return o
        if isinstance(o, list):
            for v in o:
                t = DriveClient._find_token(v)
                if t:
                    return t
        if isinstance(o, dict):
            for v in o.values():
                t = DriveClient._find_token(v)
                if t:
                    return t
        return None

    def _get(self, url: str, timeout: int = 120000):
        for attempt in range(2):
            resp = self.page.request.get(
                url, headers={'Authorization': f'Bearer {self.token()}'},
                timeout=timeout)
            if resp.status == 401 and attempt == 0:
                self._token = None  # force refresh
                continue
            return resp
        return resp

    def metadata(self, fid: str) -> dict:
        try:
            resp = self._get(f'{DRIVE_API}/{fid}?fields=id,name,mimeType,size')
            if resp.status == 200:
                return resp.json()
        except Exception as e:
            print(f'      Drive metadata error for {fid}: {e}')
        return {}

    def download(self, fid: str, out_path: Path) -> bool:
        try:
            resp = self._get(f'{DRIVE_API}/{fid}?alt=media')
            if resp.status == 200:
                body = resp.body()
                if body:
                    out_path.parent.mkdir(parents=True, exist_ok=True)
                    out_path.write_bytes(body)
                    return True
            else:
                print(f'      Drive download HTTP {resp.status} for {fid}')
        except Exception as e:
            print(f'      Drive download error for {fid}: {e}')
        return False


# ---------------------------------------------------------------------------
# RPC parsing
# ---------------------------------------------------------------------------
_IMG_MAGIC = [(b'\x89PNG', '.png'), (b'\xff\xd8\xff', '.jpg'),
              (b'GIF8', '.gif'), (b'RIFF', '.webp')]


def _img_ext(data: bytes) -> str:
    for magic, ext in _IMG_MAGIC:
        if data.startswith(magic):
            return ext
    return '.bin'


def parse_rpc(data) -> dict:
    """Parse ResolveDriveResource JSON into title + ordered entries.

    Each entry: {'role', 'text', 'thought', 'images': [bytes...],
                 'attachments': [drive_id...], 'error': str|None}
    Structural markers (validated against raw dumps):
      turn[0]  -> text (str)
      turn[1]  -> user-uploaded Drive file IDs (list[str])
      turn[8]  -> role ('user'|'model')
      turn[12] -> inline image [mime, base64] (model-generated images)
      turn[19] -> thought flag (truthy = model reasoning, excluded from output)
      turn[28] -> error message (e.g. 'An internal error has occurred.')
    """
    try:
        inner = data[0]
        title = inner[4][0] if len(inner) > 4 and inner[4] and inner[4][0] else ''
        entries = []
        for group in inner[13] if len(inner) > 13 else []:
            if not isinstance(group, list):
                continue
            for turn in group:
                if not isinstance(turn, list) or len(turn) < 20:
                    continue
                role = turn[8] if isinstance(turn[8], str) else 'unknown'
                if role not in ('user', 'model'):
                    continue
                text = turn[0] if isinstance(turn[0], str) else ''
                thought = bool(turn[19]) if len(turn) > 19 else False
                error = turn[28] if len(turn) > 28 and isinstance(turn[28], str) else ''

                images = []
                f12 = turn[12] if len(turn) > 12 else None
                if isinstance(f12, list) and len(f12) > 1 and isinstance(f12[1], str) \
                        and len(f12[1]) > 500:
                    try:
                        images.append(base64.b64decode(f12[1]))
                    except Exception:
                        pass

                attachments = []
                f1 = turn[1] if len(turn) > 1 else None
                if isinstance(f1, list):
                    attachments = [v for v in f1 if isinstance(v, str) and len(v) > 20]

                if not text.strip() and not images and not attachments:
                    continue
                entries.append({'role': role, 'text': text, 'thought': thought,
                                'images': images, 'attachments': attachments,
                                'error': error or None})
        return {'title': title, 'entries': entries}
    except Exception as e:
        return {'title': '', 'entries': [], 'error': str(e)}


# ---------------------------------------------------------------------------
# Browser helpers
# ---------------------------------------------------------------------------
def _cdp_set_window_bounds(page, state='normal', left=None, top=None):
    try:
        cdp = page.context.new_cdp_session(page)
        target = cdp.send('Browser.getWindowForTarget')
        bounds = {'windowState': state}
        if left is not None:
            bounds['left'] = left
        if top is not None:
            bounds['top'] = top
        cdp.send('Browser.setWindowBounds', {'windowId': target['windowId'], 'bounds': bounds})
        cdp.detach()
    except Exception:
        pass


def ensure_logged_in(page):
    page.goto(LIBRARY_URL, wait_until="domcontentloaded", timeout=60000)
    time.sleep(3)
    if "accounts.google.com" in page.url:
        page.bring_to_front()
        _cdp_set_window_bounds(page, state='normal', left=100, top=100)
        print("\n" + "=" * 50)
        print("  LOG IN to Google in the browser window.")
        print("  Waiting (up to 5 minutes)...")
        print("=" * 50 + "\n")
        try:
            page.wait_for_url("**/aistudio.google.com/**", timeout=300000)
            print("Logged in! Hiding window...\n")
            _cdp_set_window_bounds(page, state='minimized')
            time.sleep(3)
        except PwTimeout:
            raise RuntimeError("Login timeout")


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


# ---------------------------------------------------------------------------
# Saving
# ---------------------------------------------------------------------------
def slugify(title: str) -> str:
    return re.sub(r'[^\w\s-]', '', title)[:60].strip()


def save_chat(parsed: dict, chat_info: dict, out_dir: Path,
              drive: DriveClient | None = None) -> dict:
    """Save one chat: markdown with inline images + downloaded attachments.

    Consecutive same-role entries are merged into one section (multi-part
    answers like text -> image -> text stay together). Thought entries skipped.
    """
    title = parsed.get('title') or chat_info.get('title', '')
    slug = slugify(title) or chat_info['id'][:20]
    chat_dir = out_dir / slug
    chat_dir.mkdir(parents=True, exist_ok=True)
    md = chat_dir / f"{slug}.md"
    c = 1
    while md.exists():
        md = chat_dir / f"{slug}_{c}.md"; c += 1

    entries = [e for e in parsed.get('entries', []) if not e.get('thought')]
    had_thoughts = any(e.get('thought') for e in parsed.get('entries', []))
    errors = [e['error'] for e in parsed.get('entries', []) if e.get('error')]

    img_count = 0
    doc_count = 0
    total_imgs = total_docs = 0
    used_names: set[str] = set()

    def unique_name(name: str) -> str:
        if name not in used_names:
            used_names.add(name)
            return name
        stem, dot, ext = name.rpartition('.')
        if not dot:
            stem, ext = name, ''
        n = 1
        while True:
            cand = f'{stem}_{n}{("." + ext) if ext else ""}'
            if cand not in used_names:
                used_names.add(cand)
                return cand
            n += 1

    with open(md, 'w', encoding='utf-8') as f:
        f.write(f"# {title}\n\n**Source:** {chat_info['url']}\n\n"
                f"**Scraped:** {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n---\n\n")

        cur_role = None
        for e in entries:
            if e['role'] != cur_role:
                if cur_role is not None:
                    f.write("\n---\n\n")
                f.write(f"## {'User' if e['role'] == 'user' else 'Model'}\n\n")
                cur_role = e['role']

            if e['text'].strip():
                f.write(e['text'].strip() + "\n\n")

            for raw in e['images']:
                img_count += 1
                total_imgs += 1
                fname = unique_name(f'image_{img_count}{_img_ext(raw)}')
                (chat_dir / fname).write_bytes(raw)
                f.write(f"![{fname}]({fname})\n\n")

            for fid in e['attachments']:
                doc_count += 1
                name, ok = f'drive_file_{fid[:12]}', False
                if drive:
                    meta = drive.metadata(fid)
                    if meta.get('name'):
                        name = re.sub(r'[^\w\-_. ]', '', meta['name'])[:80].strip() or name
                    name = unique_name(name)
                    ok = drive.download(fid, chat_dir / name)
                total_docs += 1
                if ok:
                    f.write(f"**Attachment:** [{name}]({name})\n\n")
                else:
                    f.write(f"**Attachment (not downloaded):** "
                            f"[drive file {fid}](https://drive.google.com/file/d/{fid}/view)\n\n")

        if cur_role is not None:
            f.write("\n---\n")

        if errors:
            f.write(f"\n> Note: {len(errors)} turn(s) ended with an API-side error "
                    f"(e.g. {errors[0]!r}); the affected response may be incomplete.\n")
        if had_thoughts:
            f.write("\n> Model reasoning (thinking) turns were omitted.\n")

    return {'md': md, 'images': total_imgs, 'docs': total_docs,
            'entries': len(entries)}


# ---------------------------------------------------------------------------
# Legacy fallback: response interception + DOM scrape (only if replay fails)
# ---------------------------------------------------------------------------
def intercept_rpc(page, chat_url: str):
    rinfo = {}

    def _on_response(response):
        if 'ResolveDriveResource' in response.url:
            try:
                body = response.body()
                if body:
                    text = body.decode('utf-8')
                    start = min((i for i, ch in enumerate(text) if ch in '[{'),
                                default=0)
                    rinfo['data'] = json.loads(text[start:])
            except Exception:
                pass

    page.on('response', _on_response)
    try:
        with page.expect_response(lambda r: 'ResolveDriveResource' in r.url,
                                  timeout=90000) as resp_info:
            page.goto(chat_url, wait_until="domcontentloaded", timeout=60000)
        if 'data' in rinfo:
            return parse_rpc(rinfo['data'])
        try:
            return parse_rpc(resp_info.value.json())
        except Exception:
            return None
    except Exception:
        return None
    finally:
        page.remove_listener('response', _on_response)


class _HtmlToMd:
    """Minimal HTML -> Markdown converter for the DOM fallback path."""
    def __init__(self):
        self._r = []
        self._lists = []
        self._pre = False
        self._code = False

    def convert(self, html: str) -> str:
        parser = _P(self)
        parser.feed(html or '')
        parser.close()
        text = ''.join(self._r)
        text = self._links(text)
        text = self._pre_blocks(text)
        text = re.sub(r'\n{3,}', '\n\n', text)
        return text.strip()

    def _o(self, s):
        self._r.append(s)

    def _links(self, text):
        def repl(m):
            href, inner = m.group(1).strip(), m.group(2)
            inner = inner.replace('\x00L\x00', '').replace('\x00T\x00', '').replace('\x00E\x00', '')
            inner = inner.strip() or href
            return f'[{inner}]({href})' if href else inner
        return re.sub('\x00L\x00(.*?)\x00T\x00(.*?)\x00E\x00', repl, text, flags=re.DOTALL)

    def _pre_blocks(self, text):
        def repl(m):
            return f'\n\n```\n{m.group(1).rstrip()}\n```\n\n'
        return re.sub('\x00P\x00(.*?)\x00Q\x00', repl, text, flags=re.DOTALL)

    def start(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'ms-cmark-node':
            return
        if tag == 'p':
            self._o('\n\n')
        elif tag == 'br':
            self._o('\n')
        elif tag in ('h1', 'h2', 'h3', 'h4', 'h5', 'h6'):
            self._o('\n\n' + '#' * int(tag[1]) + ' ')
        elif tag in ('b', 'strong'):
            self._o('**')
        elif tag in ('i', 'em'):
            self._o('*')
        elif tag in ('s', 'del', 'strike'):
            self._o('~~')
        elif tag == 'code':
            if not self._pre:
                self._code = True
                self._o('`')
        elif tag == 'pre':
            self._pre = True
            self._o('\x00P\x00')
        elif tag == 'a':
            self._o('\x00L\x00' + attrs.get('href', '') + '\x00T\x00')
        elif tag == 'img':
            self._o(f"![{attrs.get('alt', '')}]({attrs.get('src', '')})")
        elif tag == 'ul':
            self._lists.append(('ul', 0)); self._o('\n\n')
        elif tag == 'ol':
            self._lists.append(('ol', int(attrs.get('start', '1')) - 1)); self._o('\n\n')
        elif tag == 'li':
            if self._lists:
                kind, idx = self._lists[-1]
                if kind == 'ul':
                    self._o('\n- ')
                else:
                    self._lists[-1] = (kind, idx + 1)
                    self._o(f'\n{idx + 1}. ')
        elif tag == 'blockquote':
            self._o('\n\n> ')
        elif tag == 'hr':
            self._o('\n\n---\n\n')

    def end(self, tag):
        if tag == 'ms-cmark-node':
            return
        if tag in ('b', 'strong'):
            self._o('**')
        elif tag in ('i', 'em'):
            self._o('*')
        elif tag in ('s', 'del', 'strike'):
            self._o('~~')
        elif tag == 'code':
            if not self._pre:
                self._code = False
                self._o('`')
        elif tag == 'pre':
            self._pre = False
            self._o('\x00Q\x00')
        elif tag == 'a':
            self._o('\x00E\x00')
        elif tag in ('ul', 'ol'):
            if self._lists:
                self._lists.pop()
            self._o('\n\n')
        elif tag in ('p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'blockquote'):
            self._o('\n\n')

    def data(self, d):
        if self._pre or self._code:
            self._o(d)
        else:
            self._o(d.replace('\\', '\\\\').replace('`', '\\`'))


class _P(HTMLParser):
    def __init__(self, c):
        super().__init__(convert_charrefs=True)
        self._c = c
    def handle_starttag(self, tag, attrs):
        self._c.start(tag, attrs)
    def handle_endtag(self, tag):
        self._c.end(tag)
    def handle_data(self, data):
        self._c.data(data)


def extract_dom_fallback(page, chat_dir: Path) -> dict:
    """Last-resort DOM scrape: text via cmark HTML -> Markdown, images via
    element screenshots. Thought entries filtered via turn order heuristic."""
    chat_dir.mkdir(parents=True, exist_ok=True)
    try:
        page.wait_for_selector('.chat-session-content .chat-turn-container', timeout=30000)
    except Exception:
        pass
    time.sleep(3)
    n = page.evaluate("""() => {
        const s = document.querySelector('.chat-session-content');
        return s ? s.querySelectorAll('.chat-turn-container').length : 0;
    }""")
    entries = []
    seen = set()
    img_count = 0
    for i in range(n):
        page.evaluate("""(i) => {
            const s = document.querySelector('.chat-session-content');
            const t = s?.querySelectorAll('.chat-turn-container');
            t?.[i]?.scrollIntoView({block: 'center', behavior: 'instant'});
        }""", i)
        time.sleep(0.3)
        r = page.evaluate("""(i) => {
            const t = document.querySelectorAll('.chat-turn-container')[i];
            if (!t) return null;
            const cls = (t.className || '').toLowerCase();
            const role = cls.includes(' user') ? 'user' : (cls.includes(' model') ? 'model' : 'unknown');
            const parts = [];
            t.querySelectorAll('ms-text-chunk').forEach(c => {
                const cm = c.querySelector('.cmark-node') || c;
                parts.push(cm.innerHTML || '');
            });
            const img = t.querySelector('ms-image-chunk');
            return {role, html: parts.join('\\n\\n'), hasImg: !!img};
        }""", i)
        if not r:
            continue
        md = _HtmlToMd().convert(r.get('html', ''))
        if md and len(md) > 5:
            key = md[:100]
            if key not in seen:
                seen.add(key)
                entries.append({'role': r['role'], 'text': md, 'thought': False,
                                'images': [], 'attachments': [], 'error': None})
        if r.get('hasImg'):
            img_count += 1
            handle = page.evaluate_handle("""(i) => {
                const t = document.querySelectorAll('.chat-turn-container')[i];
                return t?.querySelector('ms-image-chunk');
            }""", i)
            if handle:
                path = chat_dir / f'dom_image_{img_count}.png'
                try:
                    handle.screenshot(path=str(path))
                except Exception:
                    pass
                handle.dispose()
    # positional thought heuristic for the fallback path only
    midx = [i for i, e in enumerate(entries) if e['role'] == 'model']
    for i, e in enumerate(entries):
        if e['role'] == 'model':
            nxt = next((j for j in midx if j > i), None)
            if nxt is not None and not any(x['role'] == 'user' for x in entries[i + 1:nxt]):
                e['thought'] = True
    return {'title': '', 'entries': entries}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", help="Scrape single chat URL")
    ap.add_argument("--only", help="Only chats whose title contains this substring")
    ap.add_argument("--start", type=int, default=0, help="Start index in chat list")
    ap.add_argument("--limit", type=int, default=0, help="Max chats to process (0=all)")
    ap.add_argument("--resume", action="store_true", help="Skip chats already saved")
    ap.add_argument("--keep-raw", action="store_true", help="Also save raw RPC JSON")
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
                  "--disable-features=AutomationControlled", "--disable-dev-shm-usage",
                  "--window-position=-3000,-3000"],
            viewport={"width": 1400, "height": 900},
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        ensure_logged_in(page)
        init_replay(page)

        if args.url:
            cid = args.url.split('/prompts/')[-1].split('?')[0].split('#')[0]
            chats = [{'id': cid, 'url': args.url.split('?')[0], 'title': ''}]
        else:
            chats = get_chat_list(page)
        if args.only:
            chats = [c for c in chats if args.only.lower() in (c.get('title') or '').lower()]
        if args.start:
            chats = chats[args.start:]
        if args.limit:
            chats = chats[:args.limit]
        if not chats:
            print("No chats matched.")
            ctx.close()
            return

        if args.resume:
            before = len(chats)
            chats = [c for c in chats
                     if not (out_dir / (slugify(c.get('title') or '') or c['id'][:20])).exists()]
            skipped = before - len(chats)
            if skipped:
                print(f"--resume: skipping {skipped} already-saved chats")

        print(f"\n{'=' * 50}\n  {len(chats)} chats\n{'=' * 50}\n")
        drive = DriveClient(page)
        done = failed = fallbacks = imgs_total = docs_total = 0

        for i, chat in enumerate(chats):
            label = (chat['title'] or chat['id'])[:55]
            t0 = time.time()
            try:
                # Pass 1 (primary): replay the RPC ourselves
                status, text = rpc_call(page, 'ResolveDriveResource', [chat['id']])
                parsed = parse_rpc(json.loads(text)) if status == 200 else None
                path = 'rpc'

                # Pass 2 (fallback): legacy interception
                if parsed is None or not parsed.get('entries'):
                    print(f"[{i + 1}/{len(chats)}] {label} [intercept fallback]")
                    parsed = intercept_rpc(page, chat['url'])
                    path = 'intercept'

                # Pass 3 (last resort): DOM scrape
                if parsed is None or not parsed.get('entries'):
                    print(f"[{i + 1}/{len(chats)}] {label} [DOM fallback]")
                    page.goto(chat['url'], wait_until="domcontentloaded", timeout=60000)
                    slug = slugify(chat.get('title', '')) or chat['id'][:20]
                    parsed = extract_dom_fallback(page, out_dir / slug)
                    path = 'dom'

                if parsed is None:
                    raise RuntimeError('all extraction paths failed')
                if path != 'rpc':
                    fallbacks += 1

                chat['title'] = parsed.get('title') or chat.get('title', '')
                if args.keep_raw and path == 'rpc':
                    slug = slugify(chat['title']) or chat['id'][:20]
                    raw_dir = out_dir / slug
                    raw_dir.mkdir(parents=True, exist_ok=True)
                    (raw_dir / 'raw_rpc.json').write_text(text)

                stats = save_chat(parsed, chat, out_dir, drive=drive)
                imgs_total += stats['images']
                docs_total += stats['docs']
                users = sum(1 for e in parsed['entries']
                            if e['role'] == 'user' and not e.get('thought'))
                models = stats['entries'] - users
                chars = sum(len(e['text']) for e in parsed['entries'] if not e.get('thought'))
                extra = f", {stats['images']} img" if stats['images'] else ''
                extra += f", {stats['docs']} doc" if stats['docs'] else ''
                print(f"[{i + 1}/{len(chats)}] {label}")
                print(f"  -> {stats['entries']}t ({users}u/{models}m), "
                      f"{chars:,} chars{extra} [{path}, {time.time() - t0:.1f}s]")
                done += 1
            except Exception as e:
                print(f"[{i + 1}/{len(chats)}] {label} -> FAILED: {e}")
                traceback.print_exc()
                failed += 1

        print(f"\n{'=' * 50}")
        print(f"  Done: {done} ok, {failed} failed, {fallbacks} fallbacks")
        print(f"  Images: {imgs_total}, Documents: {docs_total}")
        print(f"  Output: {out_dir}")
        print(f"{'=' * 50}")
        ctx.close()


if __name__ == "__main__":
    main()
