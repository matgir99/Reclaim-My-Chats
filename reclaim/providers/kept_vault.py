"""Kept vault importer (import mode).

Kept (https://github.com/egroup-labs/kept, MIT) syncs Kimi/Claude/Grok/
Gemini/ChatGPT conversations into a local Markdown vault:

    ~/.kept/vault/<platform>/<date>_<title>.md

Vault file format (from Kept's vault.rs):

    ---
    id: "conv-id"
    platform: "kimi"
    title: "Chat Title"
    synced: <rfc3339>
    created_at: ...        (optional)
    updated_at: ...        (optional)
    messages: 12
    model: "..."           (optional)
    tags:
      - "kept/kimi"
    ---

    # Chat Title

    ### You — 2026-01-01 10:00        (timestamp optional)
    <!-- kept:thinking -->...<!-- /kept:thinking -->   (optional; stripped)
    <!-- kept:tools -->...<!-- /kept:tools -->         (optional; stripped)
    <markdown content, possibly with data-URI images>

    ---

    ### Assistant
    ...

This importer converts vault files into canonical Chats: thinking/tool
blocks stripped, data-URI images materialized as files, http(s) images left
as links. Titles/ids from frontmatter; rerun skips already-imported chats.
"""

from __future__ import annotations

import argparse
import base64
import re
import time
import traceback
from pathlib import Path

from ..core import config
from ..core.events import emit
from ..core.service import offline_session
from ..core.manifest import SyncState, finish_run, stats_text
from ..core.model import Chat, Turn
from ..core.writer import write_chat

PROVIDER = 'kept'

# kept platform dir -> our output folder name
PLATFORM_DIRS = {
    'kimi': 'Kimi Chat',
    'chatgpt': 'ChatGPT',
    'claude': 'Claude',
    'gemini': 'Gemini',
    'grok': 'Grok',
    'ollama': 'Ollama',
}

_FM_RE = re.compile(r'\A---\n(.*?)\n---\n', re.DOTALL)
_THINK_RE = re.compile(r'<!--\s*kept:thinking\s*-->.*?<!--\s*/kept:thinking\s*-->',
                       re.DOTALL)
_TOOLS_RE = re.compile(r'<!--\s*kept:tools\s*-->.*?<!--\s*/kept:tools\s*-->',
                       re.DOTALL)
_MSG_RE = re.compile(r'^###\s+(.+?)(?:\s+—\s+.*)?$', re.MULTILINE)
_DATAURI_RE = re.compile(r'!\[([^\]]*)\]\(data:(image/[a-zA-Z0-9.+-]+);base64,([A-Za-z0-9+/=\s]+)\)')

_EXT = {'image/png': '.png', 'image/jpeg': '.jpg', 'image/gif': '.gif',
        'image/webp': '.webp', 'image/svg+xml': '.svg'}


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Parse the small YAML subset Kept writes. Returns (meta, body)."""
    m = _FM_RE.match(text)
    if not m:
        return {}, text
    meta = {}
    for line in m.group(1).splitlines():
        mm = re.match(r'^(\w+):\s*(.*)$', line)
        if not mm:
            continue
        key, val = mm.group(1), mm.group(2).strip()
        if val.startswith('"') and val.endswith('"'):
            val = val[1:-1].replace('\\"', '"').replace('\\\\', '\\')
        meta[key] = val
    return meta, text[m.end():]


def _strip_kept_blocks(content: str) -> str:
    content = _THINK_RE.sub('', content)
    content = _TOOLS_RE.sub('', content)
    return re.sub(r'\n{3,}', '\n\n', content).strip()


def _extract_datauri_images(content: str):
    """Strip data-URI images from the text and return their bytes separately
    (the writer re-inserts them as saved files after the turn text).
    Returns (new_content, [image bytes...])."""
    images = []

    def repl(m):
        _alt, _mime, b64 = m.group(1), m.group(2), m.group(3)
        try:
            images.append(base64.b64decode(re.sub(r'\s', '', b64)))
        except Exception:
            return m.group(0)  # undecodable -> keep original link
        return ''

    return _DATAURI_RE.sub(repl, content), images


def parse_vault_file(path: Path) -> Chat | None:
    text = path.read_text(encoding='utf-8')
    meta, body = parse_frontmatter(text)
    if not meta.get('id') and '###' not in body:
        return None

    platform = meta.get('platform', path.parent.name)
    title = meta.get('title') or path.stem
    chat_id = meta.get('id', path.stem)

    turns = []
    matches = list(_MSG_RE.finditer(body))
    for i, mm in enumerate(matches):
        role_raw = mm.group(1).strip().lower()
        role = 'user' if role_raw in ('you', 'user') else 'model'
        start = mm.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        content = body[start:end]
        content = re.sub(r'\n---\s*$', '', content.strip())
        content = _strip_kept_blocks(content)
        content, images = _extract_datauri_images(content)
        if content or images:
            turns.append(Turn(role=role, text=content, images=images))

    if not turns:
        return None
    return Chat(id=chat_id, title=title, source_url='', turns=turns,
                provider=f'kept/{platform}')


def run(vault_dir: Path, out_root: Path, providers: list[str] | None = None) -> list[dict]:
    vault_dir = Path(vault_dir)
    results = []
    for platform, out_name in PLATFORM_DIRS.items():
        if providers and platform not in providers:
            continue
        pdir = vault_dir / platform
        if not pdir.is_dir():
            continue
        out_dir = Path(out_root) / out_name
        out_dir.mkdir(parents=True, exist_ok=True)
        sync = SyncState(out_dir, f'kept-{platform}')

        for md_file in sorted(pdir.glob('*.md')):
            try:
                chat = parse_vault_file(md_file)
            except Exception as e:
                results.append({'id': md_file.stem, 'title': md_file.stem,
                                'ok': False, 'error': str(e)})
                emit(None, 'error', PROVIDER, f'{platform}/{md_file.stem}', message=str(e),
                     detail=traceback.format_exc())
                continue
            if chat is None:
                continue
            label = f'{platform}/{chat.title}'
            if sync.known(chat.id):
                emit(None, 'chat_skipped', PROVIDER, label,
                     message='skip (already in the database)')
                continue
            try:
                stats = write_chat(chat, out_dir)
                sync.mark(chat.id, None, str(stats['md']))
                results.append({'id': chat.id, 'title': chat.title, 'ok': True,
                                'platform': platform,
                                **{k: stats[k] for k in ('turns', 'chars', 'images', 'docs')}})
                emit(None, 'chat_completed', PROVIDER, label, message=stats_text(stats))
            except Exception as e:
                results.append({'id': chat.id, 'title': chat.title, 'ok': False,
                                'error': str(e)})
                emit(None, 'error', PROVIDER, label, message=str(e),
                     detail=traceback.format_exc())
        sync.save()
    return results


def main(argv=None):
    ap = argparse.ArgumentParser(prog='reclaim import kept')
    ap.add_argument('vault', help='Path to the Kept vault (e.g. ~/.kept/vault)')
    ap.add_argument('--providers', help='Comma list (kimi,claude,grok,gemini,chatgpt); '
                                        'default: all found in the vault')
    ap.add_argument('-o', '--output-dir',
                    default=str(config.archive_root()))
    args = ap.parse_args(argv)
    providers = [s.strip() for s in args.providers.split(',')] if args.providers else None

    started = time.time()
    with offline_session(Path(args.output_dir), 'import kept'):
        results = run(Path(args.vault), Path(args.output_dir), providers=providers)
        if results:
            return finish_run(Path(args.output_dir), PROVIDER, results, started)
        emit(None, 'nothing_matched', PROVIDER, message='No vault conversations found.')
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
