"""Canonical folder writer — the only place that writes archive files.

Output contract (per chat):

    <out_dir>/<slug>/
    └── <slug>.md          # conversation; images/attachments inline at the
                           # position they appear in the chat

Markdown format:

    # <title>
    **Source:** <url>
    **Scraped:** <ts>
    ---
    ## User
    <text>
    **Attachment:** [name](name)
    ---
    ## Assistant
    <text>
    ![image_1.png](image_1.png)
    ---
    > notes (thoughts omitted / API errors), if any
"""

from __future__ import annotations

import re
import time
from pathlib import Path

from .model import Attachment, Chat

_IMG_MAGIC = [(b'\x89PNG', '.png'), (b'\xff\xd8\xff', '.jpg'),
              (b'GIF8', '.gif'), (b'RIFF', '.webp')]

ROLE_LABEL = {'user': 'User', 'model': 'Assistant', 'assistant': 'Assistant'}


def img_ext(data: bytes) -> str:
    for magic, ext in _IMG_MAGIC:
        if data.startswith(magic):
            return ext
    return '.bin'


def slugify(title: str) -> str:
    return re.sub(r'[^\w\s-]', '', title)[:60].strip()


def sanitize_filename(name: str) -> str:
    return re.sub(r'[^\w\-_. ]', '', name)[:80].strip()


class _NameDedup:
    def __init__(self):
        self._used: set[str] = set()

    def unique(self, name: str) -> str:
        if name not in self._used:
            self._used.add(name)
            return name
        stem, dot, ext = name.rpartition('.')
        if not dot:
            stem, ext = name, ''
        n = 1
        while True:
            cand = f'{stem}_{n}{("." + ext) if ext else ""}'
            if cand not in self._used:
                self._used.add(cand)
                return cand
            n += 1


def write_chat(chat: Chat, out_dir: Path) -> dict:
    """Write one chat folder. Returns stats dict."""
    slug = slugify(chat.title) or chat.id[:20]
    chat_dir = Path(out_dir) / slug
    chat_dir.mkdir(parents=True, exist_ok=True)
    md = chat_dir / f'{slug}.md'
    c = 1
    while md.exists():
        md = chat_dir / f'{slug}_{c}.md'
        c += 1

    dedup = _NameDedup()
    n_imgs = n_docs = n_links = 0
    ts = time.strftime('%Y-%m-%d %H:%M:%S')

    with open(md, 'w', encoding='utf-8') as f:
        f.write(f'# {chat.title}\n\n')
        if chat.source_url:
            f.write(f'**Source:** {chat.source_url}\n\n')
        f.write(f'**Scraped:** {ts}\n\n---\n\n')

        cur_role = None
        img_count = 0
        for t in chat.visible_turns():
            if t.role != cur_role:
                if cur_role is not None:
                    f.write('\n---\n\n')
                f.write(f'## {ROLE_LABEL.get(t.role, t.role)}\n\n')
                cur_role = t.role

            if t.text.strip():
                f.write(t.text.strip() + '\n\n')

            for raw in t.images:
                img_count += 1
                n_imgs += 1
                fname = dedup.unique(f'image_{img_count}{img_ext(raw)}')
                (chat_dir / fname).write_bytes(raw)
                f.write(f'![{fname}]({fname})\n\n')

            for att in t.attachments:
                fname = dedup.unique(sanitize_filename(att.filename) or 'file')
                if att.data is not None:
                    (chat_dir / fname).write_bytes(att.data)
                    if att.kind == 'image':
                        n_imgs += 1
                        f.write(f'![{fname}]({fname})\n\n')
                    else:
                        n_docs += 1
                        f.write(f'**Attachment:** [{fname}]({fname})\n\n')
                else:
                    n_links += 1
                    label = att.description or att.filename
                    f.write(f'**Attachment (not downloaded):** '
                            f'[{label}]({att.source_url})\n\n')

        if cur_role is not None:
            f.write('\n---\n')

        errors = chat.errors
        if errors:
            f.write(f"\n> Note: {len(errors)} turn(s) ended with an API-side "
                    f"error (e.g. {errors[0]!r}); the affected response may "
                    f"be incomplete.\n")
        if chat.had_thoughts:
            f.write('\n> Model reasoning (thinking) turns were omitted.\n')

    return {'md': md, 'dir': chat_dir, 'images': n_imgs, 'docs': n_docs,
            'links': n_links, 'turns': len(chat.visible_turns()),
            'chars': sum(len(t.text) for t in chat.visible_turns())}
