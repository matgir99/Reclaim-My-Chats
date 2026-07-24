"""Google AI Studio provider (scrape mode) — RPC-replay architecture.

Replays the app's own ResolveDriveResource RPC from page context
(SAPISIDHASH auth). See docs/research/ANALYSIS.md for why this beats
response interception and DOM scraping.

Turn field map (validated against raw dumps):
  turn[0]  text · turn[1]  user Drive attachment IDs · turn[8]  role
  turn[12] inline image [mime, base64] · turn[19] thought flag
  turn[28] API-side error marker
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import time
import traceback
from html.parser import HTMLParser
from pathlib import Path

from ..core import browser
from ..core.manifest import SyncState, write_manifest
from ..core.model import Attachment, Chat, Turn
from ..core.writer import img_ext, slugify, write_chat

LIBRARY_URL = 'https://aistudio.google.com/library'
PROVIDER = 'aistudio'
DRIVE_API = 'https://www.googleapis.com/drive/v3/files'

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
    page.goto(LIBRARY_URL, wait_until='domcontentloaded', timeout=60000)
    time.sleep(2)
    page.evaluate(RPC_JS)
    time.sleep(4)
    page.remove_listener('request', grab_key)


def rpc_call(page, name: str, payload: list, retries: int = 3) -> tuple[int, str]:
    last_err = None
    for attempt in range(retries):
        try:
            r = page.evaluate("(a) => window.__msRpc(a.n, a.p)",
                              {'n': name, 'p': payload})
            if r['status'] == 200:
                return r['status'], r['text']
            if r['status'] in (401, 403) and attempt < retries - 1:
                time.sleep(2)
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
        resp = None
        for attempt in range(2):
            resp = self.page.request.get(
                url, headers={'Authorization': f'Bearer {self.token()}'},
                timeout=timeout)
            if resp.status == 401 and attempt == 0:
                self._token = None
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

    def fetch(self, fid: str) -> bytes | None:
        try:
            resp = self._get(f'{DRIVE_API}/{fid}?alt=media')
            if resp.status == 200:
                return resp.body()
            print(f'      Drive download HTTP {resp.status} for {fid}')
        except Exception as e:
            print(f'      Drive download error for {fid}: {e}')
        return None


# ---------------------------------------------------------------------------
# RPC parsing (raw JSON -> entries; also used by offline mode and tests)
# ---------------------------------------------------------------------------
def parse_rpc(data) -> dict:
    """Parse ResolveDriveResource JSON into title + raw entries."""
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


def entries_to_chat(parsed: dict, chat_id: str, source_url: str,
                    drive: DriveClient | None = None) -> Chat:
    """Materialize parsed entries into a canonical Chat (downloads attachments)."""
    turns = []
    for e in parsed.get('entries', []):
        atts = []
        for fid in e['attachments']:
            name, data = f'drive_file_{fid[:12]}', None
            if drive:
                meta = drive.metadata(fid)
                if meta.get('name'):
                    name = re.sub(r'[^\w\-_. ]', '', meta['name'])[:80].strip() or name
                data = drive.fetch(fid)
            atts.append(Attachment(
                filename=name,
                kind='image' if re.search(r'\.(png|jpe?g|gif|webp)$', name, re.I) else 'document',
                data=data,
                source_url=f'https://drive.google.com/file/d/{fid}/view',
            ))
        turns.append(Turn(role=e['role'], text=e['text'], thought=e['thought'],
                          images=e['images'], attachments=atts, error=e['error']))
    return Chat(id=chat_id, title=parsed.get('title', ''), source_url=source_url,
                turns=turns, provider=PROVIDER)


# ---------------------------------------------------------------------------
# Library listing + login
# ---------------------------------------------------------------------------
def ensure_logged_in(page):
    page.goto(LIBRARY_URL, wait_until='domcontentloaded', timeout=60000)
    time.sleep(3)
    browser.interactive_login(page, 'accounts.google.com',
                              '**/aistudio.google.com/**', 'Google')


def get_chat_list(page) -> list[dict]:
    print('Scanning library...')
    for _ in range(35):
        page.evaluate('window.scrollTo(0, document.body.scrollHeight)')
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
# Fallbacks: response interception, then DOM scrape
# ---------------------------------------------------------------------------
def intercept_rpc(page, chat_url: str):
    rinfo = {}

    def _on_response(response):
        if 'ResolveDriveResource' in response.url:
            try:
                body = response.body()
                if body:
                    text = body.decode('utf-8')
                    start = min((i for i, ch in enumerate(text) if ch in '[{'), default=0)
                    rinfo['data'] = json.loads(text[start:])
            except Exception:
                pass

    page.on('response', _on_response)
    try:
        with page.expect_response(lambda r: 'ResolveDriveResource' in r.url,
                                  timeout=90000) as resp_info:
            page.goto(chat_url, wait_until='domcontentloaded', timeout=60000)
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


def extract_dom_fallback(page) -> dict:
    """Last-resort DOM scrape -> entries (text via cmark HTML, images as
    screenshot bytes). Thought flags via positional heuristic."""
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
        images = []
        if r.get('hasImg'):
            handle = page.evaluate_handle("""(i) => {
                const t = document.querySelectorAll('.chat-turn-container')[i];
                return t?.querySelector('ms-image-chunk');
            }""", i)
            if handle:
                try:
                    images.append(handle.screenshot())
                except Exception:
                    pass
                handle.dispose()
        md = _HtmlToMd().convert(r.get('html', ''))
        if (md and len(md) > 5) or images:
            key = md[:100]
            if key not in seen or images:
                seen.add(key)
                entries.append({'role': r['role'], 'text': md, 'thought': False,
                                'images': images, 'attachments': [], 'error': None})
    midx = [i for i, e in enumerate(entries) if e['role'] == 'model']
    for i, e in enumerate(entries):
        if e['role'] == 'model':
            nxt = next((j for j in midx if j > i), None)
            if nxt is not None and not any(x['role'] == 'user' for x in entries[i + 1:nxt]):
                e['thought'] = True
    return {'title': '', 'entries': entries}


# ---------------------------------------------------------------------------
# Main (scrape mode)
# ---------------------------------------------------------------------------
def run(page, chats: list[dict], out_dir: Path, keep_raw: bool = False,
        resume: bool = False) -> list[dict]:
    """Scrape the given chats. Returns per-chat result dicts for the manifest."""
    out_dir = Path(out_dir)
    drive = DriveClient(page)
    sync = SyncState(out_dir, PROVIDER)
    results = []

    for i, chat in enumerate(chats):
        label = (chat.get('title') or chat['id'])[:55]
        if resume and sync.known(chat['id']):
            print(f'[{i + 1}/{len(chats)}] {label} -> skip (already saved)')
            continue
        t0 = time.time()
        try:
            status, text = rpc_call(page, 'ResolveDriveResource', [chat['id']])
            parsed = parse_rpc(json.loads(text)) if status == 200 else None
            path = 'rpc'

            if parsed is None or not parsed.get('entries'):
                print(f'[{i + 1}/{len(chats)}] {label} [intercept fallback]')
                parsed = intercept_rpc(page, chat['url'])
                path = 'intercept'
            if parsed is None or not parsed.get('entries'):
                print(f'[{i + 1}/{len(chats)}] {label} [DOM fallback]')
                page.goto(chat['url'], wait_until='domcontentloaded', timeout=60000)
                parsed = extract_dom_fallback(page)
                path = 'dom'
            if parsed is None:
                raise RuntimeError('all extraction paths failed')

            chat['title'] = parsed.get('title') or chat.get('title', '')
            if keep_raw and path == 'rpc':
                raw_dir = out_dir / (slugify(chat['title']) or chat['id'][:20])
                raw_dir.mkdir(parents=True, exist_ok=True)
                (raw_dir / 'raw_rpc.json').write_text(text)

            canonical = entries_to_chat(parsed, chat['id'], chat['url'],
                                        drive=drive)
            stats = write_chat(canonical, out_dir)
            sync.mark(chat['id'], None, str(stats['md']))
            results.append({'id': chat['id'], 'title': chat['title'], 'ok': True,
                            'path': path, 'duration_s': round(time.time() - t0, 1),
                            **{k: stats[k] for k in ('turns', 'chars', 'images', 'docs')}})
            print(f"[{i + 1}/{len(chats)}] {label}")
            extra = f", {stats['images']} img" if stats['images'] else ''
            extra += f", {stats['docs']} doc" if stats['docs'] else ''
            print(f"  -> {stats['turns']}t, {stats['chars']:,} chars{extra} "
                  f"[{path}, {time.time() - t0:.1f}s]")
        except Exception as e:
            print(f'[{i + 1}/{len(chats)}] {label} -> FAILED: {e}')
            traceback.print_exc()
            results.append({'id': chat['id'], 'title': label, 'ok': False,
                            'error': str(e)})
    sync.save()
    return results


def main(argv=None):
    ap = argparse.ArgumentParser(prog='reclaim scrape aistudio')
    ap.add_argument('--url', help='Scrape single chat URL')
    ap.add_argument('--only', help='Only chats whose title contains this substring')
    ap.add_argument('--start', type=int, default=0)
    ap.add_argument('--limit', type=int, default=0)
    ap.add_argument('--resume', action='store_true')
    ap.add_argument('--keep-raw', action='store_true')
    ap.add_argument('-o', '--output-dir',
                    default=str(Path(__file__).resolve().parents[2] / 'Google AI Studio'))
    args = ap.parse_args(argv)
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    from playwright.sync_api import sync_playwright
    started = time.time()
    with sync_playwright() as p:
        ctx, page = browser.launch(p)
        try:
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
                print('No chats matched.')
                return 1

            print(f"\n{'=' * 50}\n  {len(chats)} chats\n{'=' * 50}\n")
            results = run(page, chats, out_dir, keep_raw=args.keep_raw,
                          resume=args.resume)
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
