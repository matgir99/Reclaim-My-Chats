"""Google Antigravity provider: conversations read from this computer.

Antigravity (the desktop app, its Visual Studio Code extension and its `agy`
command line) keeps every conversation in one folder, ~/.gemini/antigravity
(RECLAIM_ANTIGRAVITY_DIR names another one):

  conversations/<id>.db         one SQLite file per conversation: table `steps`
                                holds the steps (see antigravity_steps.py),
                                `trajectory_meta` the client that made it
  conversation_summaries.db     titles, projects, and the parent of a subagent
  annotations/<id>.pbtxt        the title Antigravity shows, also after a rename
  brain/<id>/                   the agent's artifacts (plans, reports)
  ../config/projects/<id>.json  project names

The files are only read: no browser and no login. A conversation whose file
is gone was deleted in Antigravity (or by `agy conversations clean`).
Conversations saved as `.pb` files by early versions of the desktop app are
encrypted; they are counted and reported, not saved.
"""

from __future__ import annotations

import codecs
import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager, suppress
from dataclasses import asdict, dataclass
from pathlib import Path
from types import SimpleNamespace

from ..core import engine
from ..core.events import EventSink, emit
from ..core.model import Attachment, Chat, Turn
from . import antigravity_steps as steps_mod
from .antigravity_steps import FormatError, Message, Transcript

PROVIDER = 'antigravity'
ENV_DIR = 'RECLAIM_ANTIGRAVITY_DIR'
# exa.cortex_pb.CortexTrajectorySource: the client that started a conversation.
_SOURCES = {17: 'Antigravity CLI', 22: 'Antigravity for VS Code',
            23: 'Antigravity for Visual Studio'}
_IMAGES = {'.png', '.jpg', '.jpeg', '.gif', '.webp'}
_TITLE = re.compile(r'^\s*title:\s*"((?:[^"\\]|\\.)*)"', re.MULTILINE)
_ID = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')


def data_dir() -> Path:
    """Antigravity's folder: RECLAIM_ANTIGRAVITY_DIR, else ~/.gemini/antigravity."""
    override = os.environ.get(ENV_DIR, '').strip()
    return Path(override).expanduser() if override else Path.home() / '.gemini' / 'antigravity'


def _source_file(path: Path, stack: ExitStack) -> tuple[Path, str]:
    """Which file to open, and how, without creating anything in Antigravity's folder.

    Antigravity's databases use a write-ahead log. Opened read-only, SQLite
    creates the log's `-wal` and `-shm` files when they are missing. Without
    both, nothing has the database open and no log is pending, so it is read
    as unchanging (`immutable`); with both, read-only reads the log as well;
    with only one, a temporary copy is read."""
    wal, shm = (path.with_name(f'{path.name}-{end}') for end in ('wal', 'shm'))
    if wal.exists() and shm.exists():
        return path, 'mode=ro'
    if not wal.exists() and not shm.exists():
        return path, 'mode=ro&immutable=1'
    copy = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix='reclaim-antigravity-')))
    for part in (path, wal):
        if part.exists():
            shutil.copy2(part, copy / part.name)
    return copy / path.name, 'mode=ro'


@contextmanager
def _reading(path: Path) -> Iterator[sqlite3.Connection]:
    """A read-only connection that writes nothing; SQLite errors name the file."""
    with ExitStack() as stack:
        try:
            target, options = _source_file(path, stack)
            con = sqlite3.connect(f'{target.resolve().as_uri()}?{options}', uri=True, timeout=10)
        except (OSError, sqlite3.Error) as exc:
            raise RuntimeError(f'could not open Antigravity file {path.name}: {exc}') from exc
        stack.callback(con.close)
        con.row_factory = sqlite3.Row
        try:
            yield con
        except sqlite3.Error as exc:
            raise RuntimeError(f'could not read Antigravity file {path.name}: {exc}') from exc


@dataclass
class Conversation:
    """One conversation as listed: where it is and what the database calls it."""
    id: str
    path: Path
    title: str
    project: str | None
    updated_at: str
    parent: str = ''          # the conversation that started this subagent
    parent_title: str = ''
    agent: str = ''           # the subagent's kind, e.g. 'research'
    modified: str = ''        # Antigravity's own time of the last change, for the order


def _summaries(root: Path) -> dict[str, dict]:
    path = root / 'conversation_summaries.db'
    if not path.is_file():
        return {}
    with _reading(path) as con:
        return {str(row['conversation_id']): dict(row)
                for row in con.execute('SELECT * FROM conversation_summaries')}


def _annotation_title(root: Path, cid: str) -> str:
    try:
        text = (root / 'annotations' / f'{cid}.pbtxt').read_text(encoding='utf-8')
    except (OSError, UnicodeDecodeError):
        return ''
    match = _TITLE.search(text)
    if not match:
        return ''
    raw, _ = codecs.escape_decode(match.group(1).encode('utf-8'))
    return raw.decode('utf-8', 'replace').strip()


def _project_names(root: Path) -> dict[str, str]:
    names = {}
    for path in sorted((root.parent / 'config' / 'projects').glob('*.json')):
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        name = data.get('name') if isinstance(data, dict) else None
        if isinstance(name, str) and name.strip():
            names[str(data.get('id') or path.stem)] = name.strip()
    return names


def _artifact_files(folder: Path) -> list[Path]:
    """The agent's artifacts: files at the top of the conversation's brain folder."""
    if not folder.is_dir():
        return []
    return [path for path in sorted(folder.iterdir())
            if not path.name.startswith('.') and not path.name.endswith('.metadata.json')
            and path.is_file()]


def _version(path: Path, brain: Path) -> str:
    """What identifies this version of a conversation: the last step's time and
    a fingerprint of every step (index, status, size) and artifact (size, time).
    File times of the database are not used: Antigravity rewrites the file when
    it moves its log into it, without any step changing."""
    with _reading(path) as con:
        rows = con.execute('SELECT idx, status, length(step_payload) FROM steps '
                           'ORDER BY idx').fetchall()
        last = con.execute('SELECT step_payload FROM steps ORDER BY idx DESC LIMIT 1').fetchone()
    digest = hashlib.sha256(';'.join(f'{i}:{s}:{n}' for i, s, n in rows).encode())
    for artifact in _artifact_files(brain):
        stat = artifact.stat()
        digest.update(f'|{artifact.name}:{stat.st_size}:{stat.st_mtime_ns}'.encode())
    try:
        when = steps_mod.created_at(Message(last[0])) if last else ''
    except FormatError:
        when = ''
    return f'{when or "no steps"} · {len(rows)} steps · {digest.hexdigest()[:12]}'


def _read_steps(path: Path, step_type: int | None = None) -> list[tuple[int, Message]]:
    query = 'SELECT idx, step_payload FROM steps'
    query += ' WHERE step_type = ? ORDER BY idx' if step_type is not None else ' ORDER BY idx'
    with _reading(path) as con:
        rows = con.execute(query, () if step_type is None else (step_type,)).fetchall()
    try:
        return [(int(row['idx']), Message(row['step_payload'])) for row in rows]
    except FormatError as exc:
        raise RuntimeError(f'Antigravity conversation {path.stem} is in a format this version '
                           f'cannot read ({exc}); update Reclaim My Chats') from exc


def _first_message(path: Path) -> str:
    """The first thing the user wrote, for a title; empty when it cannot be read
    (fetching that conversation then reports why)."""
    try:
        steps = _read_steps(path, steps_mod.USER_INPUT)
        texts = (steps_mod.user_text(step.message(steps_mod.STEP_USER_INPUT))
                 for _, step in steps)
        return next((' '.join(text.split())[:80] for text in texts if text), '')
    except (RuntimeError, FormatError):
        return ''


def _name_subagents(found: list[Conversation]) -> None:
    """A subagent is titled as Antigravity shows it in its parent conversation."""
    by_id = {c.id: c for c in found}
    titles: dict[str, str] = {}
    for parent_id in sorted({c.parent for c in found if c.parent in by_id}):
        with suppress(RuntimeError, FormatError):  # then the parent's title is used
            tools = _read_steps(by_id[parent_id].path, steps_mod.GENERIC)
            titles.update(steps_mod.subagent_titles([step for _, step in tools]))
    # Every name first, then the parents' names: a subagent can start subagents.
    names = {c.id: c.title for c in found}
    for conv in found:
        if conv.parent:
            kind = f'{conv.agent} subagent' if conv.agent else 'subagent'
            parent = by_id.get(conv.parent)
            name = (titles.get(conv.id) or (parent.title if parent else '')
                    or _first_message(conv.path) or conv.id)
            names[conv.id] = f'{name} ({kind})'
    for conv in found:
        if conv.parent:
            conv.parent_title = names.get(conv.parent, '')
            conv.title = names[conv.id]


def list_conversations(root: Path) -> tuple[list[Conversation], int]:
    """Every readable conversation, newest first, and the number of old `.pb` ones."""
    folder = root / 'conversations'
    summaries, projects = _summaries(root), _project_names(root)
    found = []
    for path in sorted(folder.glob('*.db')):
        try:
            updated_at = _version(path, root / 'brain' / path.stem)
        except (OSError, RuntimeError):
            if not path.exists():
                continue  # removed while the folder was being read
            updated_at = 'unreadable'  # fetched every time, so its error is reported
        meta = summaries.get(path.stem, {})
        title = _annotation_title(root, path.stem) or str(meta.get('title') or '').strip()
        found.append(Conversation(
            id=path.stem, path=path, title=title,
            project=projects.get(str(meta.get('project_id') or '')),
            updated_at=updated_at, parent=str(meta.get('parent_conversation_id') or ''),
            agent=str(meta.get('agent_name') or '').strip(),
            modified=str(meta.get('last_modified_time') or '')))
    for conv in found:
        if not conv.title and not conv.parent:
            conv.title = _first_message(conv.path)
    _name_subagents(found)
    found.sort(key=lambda c: (c.modified, c.updated_at), reverse=True)  # newest first
    readable = {c.id for c in found}
    old = sum(1 for path in folder.glob('*.pb') if path.stem not in readable)
    return found, old


def _artifacts(folder: Path) -> list[Attachment]:
    files = []
    for path in _artifact_files(folder):
        with suppress(OSError):
            files.append(Attachment(filename=path.name, data=path.read_bytes(),
                                    kind='image' if path.suffix.lower() in _IMAGES
                                    else 'document'))
    return files


def _source(path: Path) -> str:
    with _reading(path) as con:
        row = con.execute('SELECT source FROM trajectory_meta LIMIT 1').fetchone()
    return _SOURCES.get(int(row['source'] or 0) if row else 0, 'Antigravity')


def read_chat(root: Path, conv: Conversation) -> tuple[Chat, dict, int]:
    """The chat, its raw.json content, and how many artifacts it has."""
    steps = _read_steps(conv.path)
    source = _source(conv.path)
    transcript = Transcript(parent_id=conv.parent, parent_title=conv.parent_title)
    try:
        for _, step in steps:
            transcript.add(step)
        raw_steps = [steps_mod.raw_step(index, step) for index, step in steps]
    except FormatError as exc:
        raise RuntimeError(f'Antigravity conversation {conv.id} is in a format this version '
                           f'cannot read ({exc}); update Reclaim My Chats') from exc
    turns = transcript.turns()
    if steps and not any(t.text or t.images for t in turns):
        raise RuntimeError(f'Antigravity conversation {conv.id} has {len(steps)} steps but no '
                           'readable messages; its format may have changed')
    artifacts = _artifacts(root / 'brain' / conv.id)
    if artifacts:
        turns.append(Turn(role='model', text='**Artifacts**', attachments=artifacts))
    chat = Chat(id=conv.id, title=conv.title or transcript.checkpoint_title or '(untitled)',
                source_url=f'{source}, conversation {conv.id}', turns=turns,
                provider=PROVIDER, project=conv.project)
    meta = {k: str(v) if isinstance(v, Path) else v for k, v in asdict(conv).items()}
    meta['path'] = conv.path.name
    return chat, {'conversation': {**meta, 'source': source}, 'steps': raw_steps}, len(artifacts)


class Antigravity(engine.Provider):
    """Conversations Antigravity keeps on this computer; the engine does the rest."""

    name = PROVIDER
    folder = 'Google Antigravity'
    description = ('Save Google Antigravity conversations (desktop app and VS Code '
                   'extension) from this computer to the database (update by default).')
    noun = 'conversations'
    pace = 0.0
    uses_browser = False

    def present(self) -> bool:
        return (data_dir() / 'conversations').is_dir()

    def open(self, page):
        root = data_dir()
        if not (root / 'conversations').is_dir():
            raise RuntimeError(
                f'Antigravity conversations were not found in {root / "conversations"}. '
                'Antigravity (the app or its VS Code extension) creates that folder with the '
                f'first conversation; if it keeps them elsewhere, set {ENV_DIR} to its folder.')
        return SimpleNamespace(root=root, listing_complete=True, conversations=None)

    def _conversations(self, s) -> list[Conversation]:
        if s.conversations is None:
            s.conversations, old = list_conversations(s.root)
            if old:
                emit(None, 'warning', PROVIDER, message=(
                    f'{old} conversation(s) saved by an earlier Antigravity version (.pb files '
                    f'in {s.root / "conversations"}) are encrypted and were not saved'))
        return s.conversations

    def listing_text(self) -> str:
        return 'Reading Antigravity conversations on this computer...'

    def checked_text(self, refs: list[engine.ChatRef]) -> str:
        return f'{len(refs)} {self.noun} found on this computer'

    def listing(self, s) -> list[engine.ChatRef]:
        return [engine.ChatRef(id=c.id, title=c.title, updated_at=c.updated_at, data=c,
                               project=c.project)
                for c in self._conversations(s)]

    def from_url(self, s, url: str) -> engine.ChatRef:
        match = _ID.search(url.lower())
        if not match:
            raise ValueError('give an Antigravity conversation id (or the path of its .db file)')
        for conv in self._conversations(s):
            if conv.id == match.group(0):
                return engine.ChatRef(id=conv.id, title=conv.title,
                                      updated_at=conv.updated_at, data=conv)
        raise RuntimeError(f'Antigravity has no conversation {match.group(0)} on this computer')

    def fetch(self, s, ref: engine.ChatRef) -> engine.Fetched:
        conv = ref.data if isinstance(ref.data, Conversation) else self.from_url(s, ref.id).data
        chat, raw, artifacts = read_chat(s.root, conv)
        return engine.Fetched(chat, raw=raw,
                              detail=f'steps: {len(raw["steps"])} · artifacts: {artifacts}')

    def confirm_status(self, s, chat_id: str) -> str:
        """'deleted' when the conversation's file is gone from Antigravity's folder."""
        folder = s.root / 'conversations'
        if any((folder / f'{chat_id}{ext}').exists() for ext in ('.db', '.pb')):
            return 'active'
        return 'deleted' if folder.is_dir() else 'unconfirmed'


SPEC = Antigravity()


def build_parser():
    return engine.build_parser(SPEC)


def parse_args(argv=None):
    return engine.parse_args(SPEC, argv)


def run_session(page, args, sink: EventSink | None = None) -> int:
    """This provider (no browser: `page` is not used)."""
    return engine.run_session(SPEC, page, args, sink)


def main(argv=None):
    return engine.main(SPEC, argv)


if __name__ == '__main__':
    raise SystemExit(main())
