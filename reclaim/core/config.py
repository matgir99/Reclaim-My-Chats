"""User configuration: an optional `.reclaim.json` in the data folder.

Optional keys:

    {"version": 1, "providers": ["googleaistudio", "chatgpt"],
     "archive": "/path/to/database", "profile": "/path/to/login-data"}

- "providers" restricts which providers `reclaim all` and the window use.
  Providers not listed are skipped entirely: no browser window, no login
  wait. Single-provider commands (`reclaim chatgpt`) are unaffected.
- "archive" is the database folder (the key name predates the term); see
  `archive_root`. Without it the default location is used.
- "profile" is the login data folder (a browser profile).

A missing file means defaults. An unreadable or invalid file raises
`SettingsError`: falling back to defaults would silently use another
database and enable every provider.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from .fsutil import atomic_write_text

CONFIG_NAME = '.reclaim.json'
SETTINGS_VERSION = 1


class SettingsError(ValueError):
    """The settings file exists but cannot be used."""

    def __init__(self, path: Path, detail: str):
        self.path = Path(path)
        self.detail = detail
        super().__init__(f'The settings file {self.path} {detail}. '
                         'Fix the file, or reset the settings to use the defaults.')

# The single archive root under the repo. All provider output dirs live
# here. In the owner's setup this folder is a symlink to a cloud-synced
# directory (Syncthing), so the chats are available on every device while
# the repo itself stays clean (the folder is gitignored).
ARCHIVE_DIR = 'chats'


def default_root() -> Path:
    """Writable home for config, archive, and browser profile.

    Source checkouts keep their historical layout. Installed and frozen apps
    use an OS user-data directory instead of writing beside package code.
    RECLAIM_HOME can point either frontend at an existing checkout.
    """
    override = os.environ.get('RECLAIM_HOME')
    if override:
        return Path(override).expanduser().resolve()
    source = Path(__file__).resolve().parents[2]
    if not getattr(sys, 'frozen', False) and (source / '.git').exists():
        return source
    home = Path.home()
    if os.name == 'nt':
        base = Path(os.environ.get('LOCALAPPDATA') or home / 'AppData' / 'Local')
    elif sys.platform == 'darwin':
        base = home / 'Library' / 'Application Support'
    else:
        base = Path(os.environ.get('XDG_DATA_HOME') or home / '.local' / 'share')
    return base / 'ReclaimMyChats'


def default_archive(root: Path | None = None) -> Path:
    """The database location used when the settings choose none."""
    return (Path(root) if root is not None else default_root()) / ARCHIVE_DIR


def resolve_folder(root: Path, value: str) -> Path:
    """A folder typed in the settings: relative paths start at `root`."""
    return _configured_path(Path(root), value)


def _configured_path(base: Path, value: str) -> Path:
    path = Path(value).expanduser()
    return Path(os.path.normpath(path if path.is_absolute() else base / path))


def archive_root(root: Path | None = None) -> Path:
    """Where provider output dirs live.

    Default: <repo>/<ARCHIVE_DIR>. Override once per machine with the
    "archive" key in .reclaim.json — an absolute path is used as-is, a
    relative path is resolved against the repo root, and "" or "." keeps
    the pre-chats layout (provider dirs directly at the repo root). Any
    of these may be a symlink; the code never looks at the target, so a
    cloud-synced archive works for everyone.
    """
    base = Path(root) if root is not None else default_root()
    override = load(base).get('archive')
    if isinstance(override, str) and override.strip():
        return _configured_path(base, override)
    if override == '':
        return base  # explicit empty string: provider dirs at the root itself
    return base / ARCHIVE_DIR


def profile_root(root: Path | None = None) -> Path:
    """Existing checkout profile by default; optionally reuse another one."""
    base = Path(root) if root is not None else default_root()
    override = load(base).get('profile')
    if isinstance(override, str) and override.strip():
        return _configured_path(base, override)
    return base / '.playwright-profile'


def load(root: Path) -> dict:
    """The parsed settings, {} when there is no settings file.

    Raises SettingsError for an unreadable, malformed or newer file."""
    path = Path(root) / CONFIG_NAME
    try:
        text = path.read_text(encoding='utf-8')
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError) as exc:
        raise SettingsError(path, f'cannot be read ({exc})') from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SettingsError(path, f'is not valid JSON ({exc.msg} at line {exc.lineno}, '
                                  f'column {exc.colno})') from exc
    problem = _problem(data)
    if problem:
        raise SettingsError(path, problem)
    return data


def _problem(data) -> str | None:
    """Why parsed settings cannot be used, or None."""
    if not isinstance(data, dict):
        return 'must contain a JSON object, such as {"providers": ["chatgpt"]}'
    version = data.get('version', 1)
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        return 'has an invalid "version" (expected a whole number)'
    if version > SETTINGS_VERSION:
        return (f'was written by a newer version of Reclaim My Chats '
                f'(settings version {version}); update the app')
    providers = data.get('providers', [])
    if not isinstance(providers, list) or not all(isinstance(p, str) for p in providers):
        return 'has an invalid "providers" value (expected a list of provider names)'
    for key in ('archive', 'profile'):
        if key in data and not isinstance(data[key], str):
            return f'has an invalid "{key}" value (expected a folder path)'
    return None


def uses_default_database(root: Path | None = None) -> bool:
    """True when the settings do not choose a database folder."""
    base = Path(root) if root is not None else default_root()
    return 'archive' not in load(base)


def reset(root: Path) -> Path | None:
    """Move an unusable settings file aside so the defaults apply.

    Returns the backup path, or None when there was no settings file."""
    path = Path(root) / CONFIG_NAME
    if not path.exists():
        return None
    backup = path.with_name(f'{CONFIG_NAME}.invalid-{time.strftime("%Y%m%d-%H%M%S")}')
    os.replace(path, backup)
    return backup


def save(root: Path, *, providers: list[str], archive: str | None,
         profile: str | None = None) -> None:
    """Atomically update settings without discarding other config keys.

    `archive=None` removes the database choice, so the default location
    applies (and may be created on first use). `profile=''` removes the
    login data folder choice; `profile=None` leaves it unchanged."""
    if not providers or not all(isinstance(p, str) for p in providers):
        raise ValueError('select at least one provider')
    if archive is not None and not isinstance(archive, str):
        raise ValueError('archive must be a path')
    if profile is not None and not isinstance(profile, str):
        raise ValueError('profile must be a path')
    root = Path(root)
    data = _existing_for_update(root / CONFIG_NAME)
    data['version'] = SETTINGS_VERSION
    data['providers'] = providers
    _set_or_drop(data, 'archive', archive, keep_empty=True)
    if profile is not None:
        _set_or_drop(data, 'profile', profile if profile.strip() else None)
    root.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, indent=2, ensure_ascii=False) + '\n'
    atomic_write_text(root / CONFIG_NAME, text, mode=0o600)


def _existing_for_update(path: Path) -> dict:
    """Current settings to merge into; refuses to overwrite a broken file."""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise ValueError(f'cannot overwrite invalid {CONFIG_NAME}: {exc}') from exc
    if not isinstance(data, dict):
        raise ValueError(f'cannot overwrite invalid {CONFIG_NAME}')
    return data


def _set_or_drop(data: dict, key: str, value: str | None, *, keep_empty: bool = False) -> None:
    if value is None or (not value and not keep_empty):
        data.pop(key, None)
    else:
        data[key] = value


def selected_providers(root: Path, available: list[str]) -> tuple[list[str], list[str], list[str]]:
    """Split `available` per the config's "providers" list.

    Returns (chosen, skipped, unknown): providers to run, providers
    skipped by config, and config names that match no provider (typos).
    No/empty "providers" key -> everything chosen.
    """
    want = load(root).get('providers')
    if not isinstance(want, list) or not want:
        return list(available), [], []
    chosen = [p for p in available if p in want]
    skipped = [p for p in available if p not in want]
    unknown = [p for p in want if p not in available]
    return chosen, skipped, unknown
