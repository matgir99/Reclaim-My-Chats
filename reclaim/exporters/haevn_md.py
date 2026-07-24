"""Exporter: ReclaimMyChats archive -> HAEVN Markdown import zip.

HAEVN (https://github.com/aiamblichus/haevn, MIT) imports markdown files
with YAML frontmatter and HTML-comment role markers:

    ---
    title: Example Chat
    source: Gemini
    conversation_id: optional-stable-id
    created_at: 2026-03-26T16:05:00.000Z
    models_used:
      - gemini-2.5-pro
    ---

    <!-- HAEVN: role="user" -->
    User message text

    <!-- HAEVN: role="assistant" -->
    Assistant message text

Source of truth per chat folder: ``chat.json`` (canonical dump, written by
core.writer). Folders without chat.json (older archives) fall back to
parsing the markdown's ``## User`` / ``## Assistant`` sections.

Zip layout: ``<provider>/<slug>.md`` plus media under ``<provider>/<slug>/``.
HAEVN's importer documents text/structure ingestion; media files are
included alongside with relative links (best effort).
"""

from __future__ import annotations

import argparse
import json
import re
import zipfile
from pathlib import Path

SOURCE_NAMES = {
    'aistudio': 'AI Studio',
    'deepseek': 'DeepSeek',
    'chatgpt': 'ChatGPT',
}
for _p in ('kimi', 'claude', 'gemini', 'grok', 'ollama'):
    SOURCE_NAMES[f'kept/{_p}'] = _p.capitalize()


def _yaml_escape(s: str) -> str:
    return '"' + s.replace('\\', '\\\\').replace('"', '\\"') + '"'


def chat_to_haevn_md(payload: dict) -> str:
    title = payload.get('title', '(untitled)')
    source = SOURCE_NAMES.get(payload.get('provider', ''),
                              payload.get('provider', 'unknown') or 'unknown')
    lines = [
        '---',
        f'title: {_yaml_escape(title)}',
        f'source: {_yaml_escape(source)}',
        f'conversation_id: {_yaml_escape(payload.get("id", ""))}',
        '---',
        '',
    ]
    for turn in payload.get('turns', []):
        role = 'user' if turn.get('role') == 'user' else 'assistant'
        lines.append(f'<!-- HAEVN: role="{role}" -->')
        body = (turn.get('text') or '').strip()
        for fname in turn.get('images', []):
            body += f'\n\n![{fname}]({Path(fname).name})'
        for att in turn.get('attachments', []):
            if att.get('saved'):
                fname = Path(att['filename']).name
                if att.get('kind') == 'image':
                    body += f'\n\n![{fname}]({fname})'
                else:
                    body += f'\n\n**Attachment:** [{fname}]({fname})'
            else:
                label = att.get('description') or att.get('filename')
                body += (f'\n\n**Attachment (not downloaded):** '
                         f'[{label}]({att.get("source_url", "")})')
        lines.append(body)
        lines.append('')
    return '\n'.join(lines)


_MD_SECTION_RE = re.compile(r'^## (User|Assistant)\s*$', re.MULTILINE)


_DIR_PROVIDER = {'Google AI Studio': 'aistudio', 'Deepseek Chat': 'deepseek',
                 'ChatGPT': 'chatgpt', 'Kimi Chat': 'kept/kimi',
                 'Claude': 'kept/claude', 'Gemini': 'kept/gemini',
                 'Grok': 'kept/grok'}


def _fallback_payload_from_md(chat_dir: Path) -> dict | None:
    mds = sorted(chat_dir.glob('*.md'))
    if not mds:
        return None
    text = mds[0].read_text(encoding='utf-8')
    title_m = re.match(r'^# (.+)$', text, re.MULTILINE)
    src_m = re.search(r'^\*\*Source:\*\* (\S+)', text, re.MULTILINE)
    provider = _DIR_PROVIDER.get(chat_dir.parent.name, '')
    turns = []
    matches = list(_MD_SECTION_RE.finditer(text))
    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[start:end]
        body = re.sub(r'\n---\s*$', '', body.strip())
        body = re.sub(r'\n> (Note:|Model reasoning).*$', '', body,
                      flags=re.DOTALL)
        if body.strip():
            turns.append({'role': 'user' if m.group(1) == 'User' else 'model',
                          'text': body.strip(), 'images': [], 'attachments': []})
    if not turns:
        return None
    return {'id': chat_dir.name, 'title': title_m.group(1) if title_m else chat_dir.name,
            'source_url': src_m.group(1) if src_m else '',
            'provider': provider, 'turns': turns}


def export_zip(archive_root: Path, out_zip: Path) -> dict:
    archive_root = Path(archive_root)
    stats = {'chats': 0, 'media': 0, 'skipped': 0}
    media_ext = {'.png', '.jpg', '.jpeg', '.gif', '.webp', '.pdf', '.txt',
                 '.csv', '.doc', '.docx', '.xls', '.xlsx', '.bin', '.svg'}
    with zipfile.ZipFile(out_zip, 'w', zipfile.ZIP_DEFLATED) as z:
        for provider_dir in sorted(archive_root.iterdir()):
            if not provider_dir.is_dir() or provider_dir.name.startswith(('.', '__')):
                continue
            for chat_dir in sorted(provider_dir.iterdir()):
                if not chat_dir.is_dir():
                    continue
                payload = None
                cj = chat_dir / 'chat.json'
                if cj.exists():
                    try:
                        payload = json.loads(cj.read_text(encoding='utf-8'))
                    except Exception:
                        payload = None
                if payload is None:
                    payload = _fallback_payload_from_md(chat_dir)
                if payload is None:
                    stats['skipped'] += 1
                    continue
                md = chat_to_haevn_md(payload)
                slug = chat_dir.name
                z.writestr(f'{provider_dir.name}/{slug}.md', md)
                stats['chats'] += 1
                for f in chat_dir.iterdir():
                    if f.is_file() and f.suffix.lower() in media_ext:
                        z.write(f, f'{provider_dir.name}/{slug}/{f.name}')
                        stats['media'] += 1
    return stats


def main(argv=None):
    ap = argparse.ArgumentParser(prog='reclaim export haevn-md')
    ap.add_argument('archive_root', help='Archive root (contains provider folders)')
    ap.add_argument('out_zip', help='Output zip path')
    args = ap.parse_args(argv)
    stats = export_zip(Path(args.archive_root), Path(args.out_zip))
    print(f"Exported {stats['chats']} chats ({stats['media']} media files) "
          f"-> {args.out_zip}  (skipped: {stats['skipped']})")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
