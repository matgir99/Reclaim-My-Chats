"""The database folder: recognising, creating and adopting it.

A database is the folder holding one subfolder per provider. It is marked
by `.reclaim-database.json`, which records its format version. The marker
lets the app tell a database from an empty or unrelated folder, so a
missing folder (an unmounted drive, a moved sync folder) is reported
instead of silently replaced by a new, empty database.

Databases created before the marker existed are recognised by their
provider folders and records, and marked on the first update ("adopted").
Adopting also copies each chat's provider timestamp from the sync record
into its chat.json, so the sync records can later be rebuilt from the
chat folders alone.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

from .fsutil import atomic_write_text
from .writer import CHAT_FORMAT, CHAT_JSON, locate_recorded, read_chat_json

MARKER = '.reclaim-database.json'
DATABASE_FORMAT = 1

# Provider folder names inside a database, in display order.
PROVIDER_DIRS = {
    'googleaistudio': 'Google AI Studio',
    'deepseek': 'Deepseek Chat',
    'kimi': 'Kimi Chat',
    'chatgpt': 'ChatGPT',
    'claude': 'Claude',
    'googlegemini': 'Google Gemini',
}
_LEGACY_SYNC = {'googleaistudio': 'aistudio'}
# Files that do not make a folder "non-empty": sync tools, file managers, locks.
_IGNORABLE = {'.stfolder', '.stignore', '.DS_Store', 'desktop.ini', 'Thumbs.db',
              '.directory', '.localized', '.reclaim.lock'}
_RECORD_FILES = ('.reclaim_manifest.json', '.reclaim_last_attempt.json')


class DatabaseError(RuntimeError):
    """The database folder cannot be used as it is."""

    def __init__(self, path: Path, kind: str, message: str):
        self.path = Path(path)
        self.kind = kind
        super().__init__(message)


@dataclass(frozen=True)
class DatabaseState:
    """What a folder is. kind: ready, legacy, missing, empty, foreign,
    newer or file."""
    path: Path
    kind: str
    format: int | None = None

    @property
    def usable(self) -> bool:
        return self.kind in ('ready', 'legacy')

    def message(self) -> str:
        p = self.path
        return {
            'ready': f'{p} is a Reclaim My Chats database.',
            'legacy': f'{p} is a Reclaim My Chats database made by an earlier version.',
            'missing': (f'The database folder {p} was not found. If it is on an external '
                        'drive or in a synced folder, connect it and try again. Otherwise '
                        'choose another folder, or create a new database there.'),
            'empty': f'{p} is not a Reclaim My Chats database yet.',
            'foreign': (f'{p} contains other files and is not a Reclaim My Chats database. '
                        'Choose an empty folder or an existing database.'),
            'newer': (f'The database in {p} was made by a newer version of Reclaim My Chats '
                      f'(format {self.format}). Update the app to use it.'),
            'file': f'{p} is a file, not a folder.',
        }[self.kind]


def _marker_format(path: Path) -> int | None:
    try:
        data = json.loads((path / MARKER).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    value = data.get('format') if isinstance(data, dict) else None
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _has_records(path: Path) -> bool:
    """True when `path` holds provider folders written by this app."""
    for name in PROVIDER_DIRS.values():
        folder = path / name
        if not folder.is_dir():
            continue
        if any((folder / record).exists() for record in _RECORD_FILES):
            return True
        if any(folder.glob('.last_sync_*.json')):
            return True
        if any(folder.glob(f'*/{CHAT_JSON}')) or any(folder.glob(f'*/*/{CHAT_JSON}')):
            return True
    return False


def inspect(path: Path) -> DatabaseState:
    """Classify a folder without changing anything."""
    path = Path(path)
    if not path.exists():  # also a link whose target is not available
        return DatabaseState(path, 'missing')
    if not path.is_dir():
        return DatabaseState(path, 'file')
    if (path / MARKER).exists():
        fmt = _marker_format(path)
        if fmt is not None and fmt > DATABASE_FORMAT:
            return DatabaseState(path, 'newer', fmt)
        return DatabaseState(path, 'ready', fmt)
    if not any(entry.name not in _IGNORABLE for entry in path.iterdir()):
        return DatabaseState(path, 'empty')
    if _has_records(path):
        return DatabaseState(path, 'legacy')
    return DatabaseState(path, 'foreign')


def _app_version() -> str:
    from importlib.metadata import PackageNotFoundError, version
    try:
        return version('reclaimmychats')
    except PackageNotFoundError:
        return 'dev'


def _write_marker(path: Path, **extra) -> None:
    data = {'format': DATABASE_FORMAT,
            'created': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
            'created_by': f'reclaimmychats {_app_version()}', **extra}
    atomic_write_text(path / MARKER, json.dumps(data, indent=2) + '\n')


def init(path: Path) -> DatabaseState:
    """Create a database in `path`, or mark an existing one.

    Creates the folder if needed. Refuses a folder with unrelated files, a
    newer database and a link whose target is not available."""
    path = Path(path)
    state = inspect(path)
    if state.kind == 'ready':
        return state
    if state.kind == 'legacy':
        adopt(path)
        return inspect(path)
    if state.kind in ('foreign', 'newer', 'file'):
        raise DatabaseError(path, state.kind, state.message())
    if os.path.islink(path):
        target = os.readlink(path)
        raise DatabaseError(path, 'missing',
                            f'{path} links to {target}, which is not available. Connect '
                            'the drive or synced folder, or choose another folder.')
    path.mkdir(parents=True, exist_ok=True)
    _write_marker(path)
    return inspect(path)


def adopt(path: Path) -> int:
    """Mark a database made before markers existed; returns the number of
    chat.json files that received their provider timestamp."""
    path = Path(path)
    count = backfill(path)
    _write_marker(path, adopted=True)
    return count


def _sync_records(folder: Path, provider: str) -> dict:
    for name in (provider, _LEGACY_SYNC.get(provider)):
        if not name:
            continue
        try:
            data = json.loads((folder / f'.last_sync_{name}.json').read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            return data
    return {}


def _with_metadata(payload: dict, record: dict, markdown: str) -> dict:
    """chat.json with the metadata of this format, keeping the turns last."""
    turns = payload.get('turns', [])
    front = {k: v for k, v in payload.items() if k != 'turns'}
    front.setdefault('provider_updated_at', record.get('updated_at'))
    front.setdefault('state', 'active')
    front.setdefault('markdown', markdown)
    return {'format': CHAT_FORMAT, **{k: v for k, v in front.items() if k != 'format'},
            'turns': turns}


def backfill(path: Path) -> int:
    """Copy provider timestamps from the sync records into older chat.json
    files that lack them. Adds fields only; never removes or changes others."""
    count = 0
    for provider, name in PROVIDER_DIRS.items():
        folder = Path(path) / name
        for chat_id, record in _sync_records(folder, provider).items():
            if not isinstance(record, dict) or not isinstance(record.get('md'), str):
                continue
            md = locate_recorded(folder, record['md'])
            payload = read_chat_json(md.parent)
            if not payload or payload.get('id') != chat_id or 'provider_updated_at' in payload:
                continue
            updated = _with_metadata(payload, record, md.name)
            atomic_write_text(md.parent / CHAT_JSON,
                              json.dumps(updated, indent=2, ensure_ascii=False, default=str))
            count += 1
    return count


def prepare(path: Path, *, create: bool = False, readonly: bool = False) -> DatabaseState:
    """Make sure `path` can be used as the database before a run.

    `create` allows creating a missing or empty database (only for the
    default location on first use). `readonly` (previews) never writes: a
    database made by an earlier version is used as it is, and a missing
    default database is treated as empty. Raises DatabaseError otherwise."""
    state = inspect(path)
    if state.kind == 'ready':
        return state
    if state.kind == 'legacy':
        if not readonly:
            count = adopt(state.path)
            print(f'Recognised the existing database in {state.path} and marked it '
                  f'(timestamps recorded in {count} chats).')
            return inspect(state.path)
        return state
    if state.kind in ('missing', 'empty') and create:
        return state if readonly else init(state.path)
    raise DatabaseError(state.path, state.kind, state.message())
