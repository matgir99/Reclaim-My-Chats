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

import base64
import json
import re
import time
from html.parser import HTMLParser
from types import SimpleNamespace
from urllib.parse import urlsplit

from ..core import browser, engine
from ..core.events import EventSink
from ..core.model import Attachment, Chat, Turn

LIBRARY_URL = 'https://aistudio.google.com/library'
PROVIDER = 'googleaistudio'
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
        url = urlsplit(req.url)
        if url.scheme == 'https' and url.hostname == 'alkalimakersuite-pa.clients6.google.com':
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
        raise RuntimeError('unreachable')

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
                              browser.on_host('aistudio.google.com'), 'Google')


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
    return [link for link in links if not (link['url'] in seen or seen.add(link['url']))]


def list_prompts(page) -> list[dict]:
    """Fast library listing via ListPrompts replay: id, title, updated_at
    (epoch seconds). Falls back to DOM scraping if the RPC shape changes."""
    status, text = rpc_call(page, 'ListPrompts', [])
    if status != 200:
        raise RuntimeError(f'ListPrompts HTTP {status}')
    data = json.loads(text)
    out = []
    for entry in data[0] if data and isinstance(data[0], list) else []:
        try:
            pid = entry[0]
            if not isinstance(pid, str) or not pid.startswith('prompts/'):
                continue
            meta = entry[4] or []
            title = meta[0] if meta and isinstance(meta[0], str) else ''
            updated = None
            ts = meta[4] if len(meta) > 4 else None
            if isinstance(ts, list) and ts and isinstance(ts[0], list) and ts[0]:
                updated = ts[0][0]
            cid = pid.split('/', 1)[1]
            out.append({'id': cid,
                        'url': f'https://aistudio.google.com/prompts/{cid}',
                        'title': title, 'updated_at': updated})
        except Exception:
            continue
    if not out:
        raise RuntimeError('ListPrompts returned no prompts (shape changed?)')
    return out


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
            self._lists.append(('ul', 0))
            self._o('\n\n')
        elif tag == 'ol':
            self._lists.append(('ol', int(attrs.get('start', '1')) - 1))
            self._o('\n\n')
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
class GoogleAIStudio(engine.Provider):
    """What only AI Studio does; the engine does the rest."""

    name = PROVIDER
    folder = 'Google AI Studio'
    description = 'Save Google AI Studio chats to the database (update by default).'
    pace = 0

    def open(self, page):
        ensure_logged_in(page)
        init_replay(page)
        return SimpleNamespace(page=page, drive=DriveClient(page))

    def listing(self, s) -> list[engine.ChatRef]:
        try:
            chats = list_prompts(s.page)
        except Exception as e:
            print(f'ListPrompts failed ({e}); falling back to DOM scan')
            chats = get_chat_list(s.page)
        return [engine.ChatRef(id=c['id'], title=c.get('title', ''),
                               updated_at=c.get('updated_at'), data=c) for c in chats]

    def from_url(self, s, url: str) -> engine.ChatRef:
        cid = url.split('/prompts/')[-1].split('?')[0].split('#')[0]
        return engine.ChatRef(id=cid, data={'id': cid, 'url': url.split('?')[0], 'title': ''})

    def fetch(self, s, ref: engine.ChatRef) -> engine.Fetched:
        url = (ref.data or {}).get('url') or f'https://aistudio.google.com/prompts/{ref.id}'
        status, text = rpc_call(s.page, 'ResolveDriveResource', [ref.id])
        parsed = parse_rpc(json.loads(text)) if status == 200 else None
        path = 'rpc'
        if parsed is None or not parsed.get('entries'):
            parsed = intercept_rpc(s.page, url)
            path = 'intercept'
        if parsed is None or not parsed.get('entries'):
            s.page.goto(url, wait_until='domcontentloaded', timeout=60000)
            parsed = extract_dom_fallback(s.page)
            path = 'dom'
        if parsed is None:
            raise RuntimeError('all extraction paths failed')
        chat = entries_to_chat(parsed, ref.id, url, drive=s.drive)
        raw = None
        if path == 'rpc':
            try:
                raw = json.loads(text)
            except ValueError as e:
                print(f'  warning: {ref.title or ref.id}: raw.json was not saved: {e}')
        return engine.Fetched(chat, raw=raw, detail=f'path: {path}', extra={'path': path})


SPEC = GoogleAIStudio()


def build_parser():
    return engine.build_parser(SPEC)


def parse_args(argv=None):
    return engine.parse_args(SPEC, argv)


def run_session(page, args, sink: EventSink | None = None) -> int:
    """This provider on an already opened page (window and command line)."""
    return engine.run_session(SPEC, page, args, sink)


def main(argv=None):
    return engine.main(SPEC, argv)


if __name__ == '__main__':
    raise SystemExit(main())
