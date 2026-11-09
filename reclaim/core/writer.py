"""Canonical folder writer: the only place that writes chat folders.

Output contract (per chat):

    <out_dir>/[<project>/]<folder>/
    ├── <folder>.md        # conversation; images/attachments inline at the
    │                      # position they appear in the chat
    ├── chat.json          # canonical data plus metadata: format, provider
    │                      # timestamp, save time, state, Markdown file name
    ├── raw.json           # the provider's response, when saved
    └── images and attachments

A folder belongs to exactly one chat: the id in its chat.json. Different
chats with the same title get "Title", "Title (2)", "Title (3)" and so on.
Names are valid on Windows, macOS and Linux. Every file is written
atomically, so an interruption never leaves a half-written file.

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

import json
import re
import time
from pathlib import Path

from .fsutil import atomic_write_bytes, atomic_write_text
from .model import Chat

CHAT_FORMAT = 1  # chat.json format written by this version

_IMG_MAGIC = [(b'\x89PNG', '.png'), (b'\xff\xd8\xff', '.jpg'),
              (b'GIF8', '.gif'), (b'RIFF', '.webp')]

ROLE_LABEL = {'user': 'User', 'model': 'Assistant', 'assistant': 'Assistant'}

# Names Windows cannot store, and folder names the database keeps for itself.
_WINDOWS_RESERVED = {'CON', 'PRN', 'AUX', 'NUL',
                     *(f'COM{i}' for i in range(1, 10)), *(f'LPT{i}' for i in range(1, 10))}
_DATABASE_RESERVED = {'_ARCHIVED', '_DELETED'}
CHAT_JSON = 'chat.json'
_GENERATED = (CHAT_JSON, 'raw.json')
_RAW_PLACEHOLDER = '<omitted: media bytes saved as files in this folder>'


def img_ext(data: bytes) -> str:
    for magic, ext in _IMG_MAGIC:
        if data.startswith(magic):
            return ext
    return '.bin'


def _portable(name: str, *, folder: bool) -> str:
    """One path component that Windows, macOS and Linux all accept."""
    name = name.rstrip(' .')
    stem = name.split('.', 1)[0].upper()
    if stem in _WINDOWS_RESERVED or (folder and name.upper() in _DATABASE_RESERVED):
        name = f'{name}_' if folder else f'_{name}'
    return name


def slugify(title: str) -> str:
    return _portable(re.sub(r'[^\w\s-]', '', title)[:60].strip(), folder=True)


def sanitize_filename(name: str) -> str:
    return _portable(re.sub(r'[^\w\-. ]', '', name)[:80].strip(), folder=False)


class _NameDedup:
    """Unique file names within one chat folder, compared without case
    (Windows and macOS treat Image.png and image.png as the same file)."""

    def __init__(self, reserved=()):
        self._used: set[str] = {name.casefold() for name in reserved}

    def unique(self, name: str) -> str:
        if self._claim(name):
            return name
        stem, dot, ext = name.rpartition('.')
        if not dot:
            stem, ext = name, ''
        n = 1
        while True:
            cand = f'{stem}_{n}{("." + ext) if ext else ""}'
            if self._claim(cand):
                return cand
            n += 1

    def _claim(self, name: str) -> bool:
        key = name.casefold()
        if key in self._used:
            return False
        self._used.add(key)
        return True


def read_chat_json(chat_dir: Path) -> dict | None:
    """The folder's chat.json, or None when absent or unreadable."""
    try:
        payload = json.loads((Path(chat_dir) / CHAT_JSON).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _same_chat(chat_dir: Path, chat_id: str) -> bool:
    """True when this folder's chat.json belongs to `chat_id`."""
    payload = read_chat_json(chat_dir)
    return payload is not None and payload.get('id') == chat_id


def media_names(payload: dict) -> set[str]:
    """Files a chat.json lists as its own images and saved attachments."""
    names: set[str] = set()
    for turn in payload.get('turns') or []:
        if not isinstance(turn, dict):
            continue
        names.update(n for n in turn.get('images') or [] if isinstance(n, str))
        for att in turn.get('attachments') or []:
            if isinstance(att, dict) and att.get('saved') and isinstance(att.get('filename'), str):
                names.add(att['filename'])
    return names


def locate_recorded(out_dir: Path, md: str) -> Path:
    """A Markdown path from a sync record, inside this provider folder.

    Records store paths relative to the provider folder. Older records are
    absolute and break when the database moves; rebase them by the provider
    folder's name."""
    out_dir = Path(out_dir)
    path = Path(md)
    if not path.is_absolute():
        return out_dir / path
    parts = path.parts
    # The provider folder's name can recur below it (a ChatGPT project named
    # "ChatGPT"), or above it in the old location. Prefer the candidate that
    # exists; otherwise cut at the last occurrence.
    candidates = [out_dir.joinpath(*parts[i + 1:])
                  for i, part in enumerate(parts[:-1]) if part == out_dir.name]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[-1] if candidates else path


def _chat_folder(parent: Path, slug: str, chat_id: str) -> Path:
    """The folder for this chat: its existing one, or the first free name.

    A folder whose chat.json names another chat is never reused. A folder
    without a readable chat.json (an interrupted first save) is reused."""
    n = 1
    while True:
        candidate = parent / (slug if n == 1 else f'{slug} ({n})')
        payload = read_chat_json(candidate)
        if payload is None or payload.get('id') == chat_id:
            return candidate
        n += 1


def _written_for(md: Path, chat: Chat) -> bool:
    """True when `md` is Markdown this app wrote for `chat` (its Source line)."""
    if not chat.source_url:
        return False
    try:
        with open(md, encoding='utf-8', errors='replace') as stream:
            head = stream.read(2048)
    except OSError:
        return False
    return f'\n**Source:** {chat.source_url}\n' in head


def _remove_stale_markdown(chat_dir: Path, md: Path, chat: Chat, previous: dict | None) -> None:
    """Delete Markdown this chat left under an older name.

    Only the Markdown recorded in chat.json, or a `<name>_<n>.md` copy that
    older versions wrote for this same chat (identified by its Source line),
    is removed. Attachments, including uploaded Markdown whose download
    failed later, and Markdown of another chat are never touched."""
    previous = previous or {}
    keep = media_names(previous)
    recorded = previous.get('markdown')
    legacy = re.compile(re.escape(md.stem) + r'_\d+\.md')
    for path in chat_dir.glob('*.md'):
        if path == md or path.name in keep:
            continue
        if path.name == recorded or (legacy.fullmatch(path.name) and _written_for(path, chat)):
            path.unlink()


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return path.resolve() != root.resolve()


def _only_this_chat(entries: list[Path], markdown: str, own: set[str]) -> bool:
    """No subfolders and no Markdown except this chat's own files."""
    return not any(e.is_dir() or (e.suffix == '.md' and e.name != markdown and e.name not in own)
                   for e in entries)


def _hand_over(entries: list[Path], new_dir: Path, *, generated: set[str]) -> None:
    """Delete generated files; move media the new folder lacks; drop identical copies."""
    for entry in entries:
        target = new_dir / entry.name
        if entry.name in generated:
            entry.unlink()
        elif not target.exists():
            entry.rename(target)
        elif target.is_file() and target.read_bytes() == entry.read_bytes():
            entry.unlink()


def retire_previous(out_dir: Path, chat_id: str, previous_md: Path, current_md: Path) -> bool:
    """Remove the folder a chat was saved in before it moved.

    A renamed chat, a chat moved into a project, or a renamed project gets a
    new folder; the old one would otherwise remain as a stale duplicate.
    Only a folder whose chat.json has the same id, and whose Markdown files
    are `previous_md` and the chat's own attachments, is touched. Its
    Markdown, chat.json and raw.json are deleted; media absent from the
    current folder is moved there, and identical copies are deleted. A
    conflicting file keeps the old folder. Returns True when the old folder
    was removed.
    """
    out_dir, old_dir, new_dir = Path(out_dir), Path(previous_md).parent, Path(current_md).parent
    if (not old_dir.is_dir() or not _inside(old_dir, out_dir) or not _inside(new_dir, out_dir)
            or old_dir.resolve() == new_dir.resolve()):
        return False
    previous = read_chat_json(old_dir)
    if previous is None or previous.get('id') != chat_id:
        return False
    entries = list(old_dir.iterdir())
    if not _only_this_chat(entries, Path(previous_md).name, media_names(previous)):
        return False
    _hand_over(entries, new_dir, generated={Path(previous_md).name, *_GENERATED})
    if any(old_dir.iterdir()):
        return False
    old_dir.rmdir()
    parent = old_dir.parent
    if _inside(parent, out_dir) and not any(parent.iterdir()):
        parent.rmdir()  # an emptied project folder
    return True


def write_raw(chat_dir: Path, obj, max_str: int = 200_000) -> Path:
    """Write raw.json: the provider's original response, with oversized
    (media/base64) string payloads replaced by a placeholder — images are
    already saved as files, so duplicating them here is waste.
    """
    def strip(o):
        if isinstance(o, str):
            return _RAW_PLACEHOLDER if len(o) > max_str else o
        if isinstance(o, list):
            return [strip(v) for v in o]
        if isinstance(o, dict):
            return {k: strip(v) for k, v in o.items()}
        return o

    path = Path(chat_dir) / 'raw.json'
    atomic_write_text(path, json.dumps(strip(obj), ensure_ascii=False, default=str))
    return path


def _dump_chat_json(chat: Chat, chat_dir: Path, saved: list[dict], md_name: str,
                    updated_at) -> None:
    """Write chat.json last: its presence marks a complete, owned folder."""
    payload = {
        'format': CHAT_FORMAT,
        'id': chat.id,
        'title': chat.title,
        'source_url': chat.source_url,
        'provider': chat.provider,
        'project': chat.project,
        'provider_updated_at': updated_at,
        'saved_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'state': 'active',
        'markdown': md_name,
        'had_thoughts': chat.had_thoughts,
        'errors': chat.errors,
        'turns': saved,
    }
    atomic_write_text(chat_dir / CHAT_JSON,
                      json.dumps(payload, indent=2, ensure_ascii=False, default=str))


class _Media:
    """Writes one chat's images and attachments and their Markdown lines."""

    def __init__(self, chat_dir: Path, md_name: str):
        self.dir = chat_dir
        self.dedup = _NameDedup(reserved=(md_name, *_GENERATED))
        self.images = self.docs = self.links = 0
        self._inline = 0  # names image_1, image_2 … count inline images only

    def image(self, raw: bytes, rec: dict, out: list[str]) -> None:
        self.images += 1
        self._inline += 1
        fname = self.dedup.unique(f'image_{self._inline}{img_ext(raw)}')
        atomic_write_bytes(self.dir / fname, raw)
        rec['images'].append(fname)
        out.append(f'![{fname}]({fname})\n\n')

    def attachment(self, att, rec: dict, out: list[str]) -> None:
        if att.data is None:
            self.links += 1
            label = att.description or att.filename
            out.append(f'**Attachment (not downloaded):** [{label}]({att.source_url})\n\n')
            rec['attachments'].append({
                'filename': att.filename, 'kind': att.kind, 'saved': False,
                'source_url': att.source_url, 'description': att.description})
            return
        fname = self.dedup.unique(sanitize_filename(att.filename) or 'file')
        atomic_write_bytes(self.dir / fname, att.data)
        if att.kind == 'image':
            self.images += 1
            out.append(f'![{fname}]({fname})\n\n')
        else:
            self.docs += 1
            out.append(f'**Attachment:** [{fname}]({fname})\n\n')
        rec['attachments'].append({'filename': fname, 'kind': att.kind, 'saved': True})


def _header(chat: Chat) -> list[str]:
    out = [f'# {chat.title}\n\n']
    if chat.project:
        out.append(f'**Project:** {chat.project}\n\n')
    if chat.source_url:
        out.append(f'**Source:** {chat.source_url}\n\n')
    out.append(f'**Scraped:** {time.strftime("%Y-%m-%d %H:%M:%S")}\n\n---\n\n')
    return out


def _footer(chat: Chat) -> list[str]:
    out = []
    if chat.errors:
        out.append(f"\n> Note: {len(chat.errors)} turn(s) ended with an API-side "
                   f"error (e.g. {chat.errors[0]!r}); the affected response may "
                   f"be incomplete.\n")
    if chat.had_thoughts:
        out.append('\n> Model reasoning (thinking) turns were omitted.\n')
    return out


def write_chat(chat: Chat, out_dir: Path, *, updated_at=None) -> dict:
    """Write one chat folder. Returns stats dict.

    `updated_at` is the provider's own timestamp for this version of the
    chat; it is stored in chat.json so sync records can be rebuilt."""
    slug = slugify(chat.title) or slugify(chat.id)[:20] or 'chat'
    parent = Path(out_dir)
    if chat.project:
        parent = parent / (slugify(chat.project) or 'Project')
    chat_dir = _chat_folder(parent, slug, chat.id)
    chat_dir.mkdir(parents=True, exist_ok=True)
    md = chat_dir / f'{chat_dir.name}.md'
    _remove_stale_markdown(chat_dir, md, chat, read_chat_json(chat_dir))

    media = _Media(chat_dir, md.name)
    saved: list[dict] = []   # per-turn saved media, for chat.json
    out = _header(chat)
    cur_role = None
    visible = chat.visible_turns()
    for t in visible:
        if t.role != cur_role:
            out.append('\n---\n\n' if cur_role is not None else '')
            out.append(f'## {ROLE_LABEL.get(t.role, t.role)}\n\n')
            cur_role = t.role
        rec: dict = {'role': t.role, 'text': t.text, 'images': [], 'attachments': []}
        saved.append(rec)
        if t.text.strip():
            out.append(t.text.strip() + '\n\n')
        for raw in t.images:
            media.image(raw, rec, out)
        for att in t.attachments:
            media.attachment(att, rec, out)
    if cur_role is not None:
        out.append('\n---\n')
    out.extend(_footer(chat))

    atomic_write_text(md, ''.join(out))
    # Canonical data dump for exporters / offline re-processing.
    _dump_chat_json(chat, chat_dir, saved, md.name, updated_at)

    return {'md': md, 'dir': chat_dir, 'images': media.images, 'docs': media.docs,
            'links': media.links, 'turns': len(visible),
            'chars': sum(len(t.text) for t in visible)}
