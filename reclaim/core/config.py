"""User configuration: an optional `.reclaim.json` in the data folder.

Optional keys (settings version 2):

    {"version": 2, "providers": ["googleaistudio", "chatgpt"],
     "archive": "/path/to/database", "profile": "/path/to/login-data",
     "kimi_region": "international", "chrome_path": "/usr/bin/google-chrome",
     "login_timeout": 900}

- "providers" restricts which providers `reclaim all` and the window use.
  Providers not listed are skipped entirely: no browser window, no login
  wait. Single-provider commands (`reclaim chatgpt`) are unaffected.
- "archive" is the database folder (the key name predates the term); see
  `archive_root`. Without it the default location is used.
- "profile" is the login data folder (a browser profile).
- "kimi_region" chooses "international" (kimi.ai) or "mainland" (kimi.com).
  `RECLAIM_KIMI_URL` overrides it for one command.
- "chrome_path" chooses the Chrome/Chromium executable.
  `RECLAIM_CHROME_PATH` overrides it for one command.
- "login_timeout" is the seconds to wait for a manual login (minimum 30,
  default 900). `RECLAIM_LOGIN_TIMEOUT` overrides it for one command.

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
from typing import Any
from urllib.parse import urlsplit

from .fsutil import atomic_write_text

CONFIG_NAME = '.reclaim.json'
SETTINGS_VERSION = 2
KIMI_REGIONS = {
    'international': 'https://www.kimi.ai',
    'mainland': 'https://www.kimi.com',
}
DEFAULT_LOGIN_TIMEOUT = 900


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


def _is_kimi_mainland(url: str) -> bool:
    host = (urlsplit(url.strip()).hostname or '').lower()
    return host == 'kimi.com' or host.endswith('.kimi.com')


def migrate(data: dict) -> dict:
    """Upgrade validated settings to `SETTINGS_VERSION`."""
    migrated = dict(data)
    kimi_url = migrated.pop('kimi_url', None)
    if isinstance(kimi_url, str) and 'kimi_region' not in migrated:
        migrated['kimi_region'] = 'mainland' if _is_kimi_mainland(kimi_url) else 'international'
    migrated['version'] = SETTINGS_VERSION
    return migrated


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
    return migrate(data)


def _version_problem(version: Any) -> str | None:
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        return 'has an invalid "version" (expected a whole number)'
    if version > SETTINGS_VERSION:
        return (f'was written by a newer version of Reclaim My Chats '
                f'(settings version {version}); update the app')
    return None


def _field_problem(data: dict) -> str | None:
    providers = data.get('providers', [])
    if not isinstance(providers, list) or not all(isinstance(p, str) for p in providers):
        return 'has an invalid "providers" value (expected a list of provider names)'
    for key in ('archive', 'profile'):
        if key in data and not isinstance(data[key], str):
            return f'has an invalid "{key}" value (expected a folder path)'
    return None


def _flag_problem(data: dict) -> str | None:
    for key in ('check_on_startup', 'start_at_login'):
        if key in data and not isinstance(data[key], bool):
            return f'has an invalid "{key}" value (expected true or false)'
    return None


def _runtime_problem(data: dict) -> str | None:
    if 'chrome_path' in data and not isinstance(data['chrome_path'], str):
        return 'has an invalid "chrome_path" value (expected a file path)'
    if 'kimi_region' in data and data['kimi_region'] not in KIMI_REGIONS:
        return 'has an invalid "kimi_region" value (expected "international" or "mainland")'
    if 'login_timeout' in data:
        timeout = data['login_timeout']
        if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout < 30:
            return 'has an invalid "login_timeout" value (expected at least 30 seconds)'
    return _flag_problem(data)


def _problem(data) -> str | None:
    """Why parsed settings cannot be used, or None."""
    if not isinstance(data, dict):
        return 'must contain a JSON object, such as {"providers": ["chatgpt"]}'
    return _version_problem(data.get('version', 1)) or _field_problem(data) or _runtime_problem(data)


def kimi_region(root: Path | None = None) -> str:
    """Configured Kimi region ('international' or 'mainland')."""
    base = Path(root) if root is not None else default_root()
    region = load(base).get('kimi_region')
    return region if isinstance(region, str) and region in KIMI_REGIONS else 'international'


def kimi_base_url(root: Path | None = None) -> str:
    """Kimi web URL; `RECLAIM_KIMI_URL` overrides the saved region setting."""
    override = os.environ.get('RECLAIM_KIMI_URL', '').strip().rstrip('/')
    if override:
        return override
    return KIMI_REGIONS[kimi_region(root)]


def chrome_path(root: Path | None = None) -> str | None:
    """Configured browser executable; `RECLAIM_CHROME_PATH` overrides it."""
    override = os.environ.get('RECLAIM_CHROME_PATH', '').strip()
    if override:
        return override
    base = Path(root) if root is not None else default_root()
    configured = load(base).get('chrome_path')
    return configured.strip() if isinstance(configured, str) and configured.strip() else None


def login_timeout(root: Path | None = None) -> int:
    """Seconds to wait for a browser login; `RECLAIM_LOGIN_TIMEOUT` overrides it."""
    override = os.environ.get('RECLAIM_LOGIN_TIMEOUT', '').strip()
    if override:
        return int(override)
    base = Path(root) if root is not None else default_root()
    configured = load(base).get('login_timeout')
    return int(configured) if isinstance(configured, int) else DEFAULT_LOGIN_TIMEOUT


def check_on_startup(root: Path | None = None) -> bool:
    """True when the GUI should check for updates non-interactively on startup."""
    base = Path(root) if root is not None else default_root()
    return bool(load(base).get('check_on_startup', False))


def start_at_login(root: Path | None = None) -> bool:
    """True when the desktop app is configured to launch on OS login."""
    base = Path(root) if root is not None else default_root()
    return bool(load(base).get('start_at_login', False))


def uses_default_database(root: Path | None = None) -> bool:
    """True when the settings do not choose a database folder."""
    base = Path(root) if root is not None else default_root()
    return 'archive' not in load(base)


def needs_first_run_setup(root: Path | None = None) -> bool:
    """True when neither settings nor the default database marker exists yet."""
    base = Path(root) if root is not None else default_root()
    return not (base / CONFIG_NAME).exists() and not (default_archive(base) / '.reclaim-database.json').exists()


def reset(root: Path) -> Path | None:
    """Move an unusable settings file aside so the defaults apply.

    Returns the backup path, or None when there was no settings file."""
    path = Path(root) / CONFIG_NAME
    if not path.exists():
        return None
    backup = path.with_name(f'{CONFIG_NAME}.invalid-{time.strftime("%Y%m%d-%H%M%S")}')
    os.replace(path, backup)
    return backup


def _validate_extra(*, kimi_region: str | None, login_timeout: int | None) -> None:
    if kimi_region is not None and kimi_region not in KIMI_REGIONS:
        raise ValueError('invalid Kimi region')
    if login_timeout is not None and (
            not isinstance(login_timeout, int) or isinstance(login_timeout, bool)
            or login_timeout < 30):
        raise ValueError('login_timeout must be at least 30 seconds')


def _apply_optional(data: dict, *, profile: str | None, kimi_region: str | None,
                    chrome_path: str | None, login_timeout: int | None,
                    check_on_startup: bool | None = None,
                    start_at_login: bool | None = None) -> None:
    if profile is not None:
        _set_or_drop(data, 'profile', profile if profile.strip() else None)
    if kimi_region is not None:
        _set_or_drop(data, 'kimi_region', None if kimi_region == 'international' else kimi_region)
    if chrome_path is not None:
        _set_or_drop(data, 'chrome_path', chrome_path.strip() or None)
    if login_timeout is not None:
        _set_or_drop(data, 'login_timeout',
                     None if login_timeout == DEFAULT_LOGIN_TIMEOUT else login_timeout)
    if check_on_startup is not None:
        _set_or_drop(data, 'check_on_startup', True if check_on_startup else None)
    if start_at_login is not None:
        _set_or_drop(data, 'start_at_login', True if start_at_login else None)


def save(root: Path, *, providers: list[str], archive: str | None,
         profile: str | None = None, kimi_region: str | None = None,
         chrome_path: str | None = None, login_timeout: int | None = None,
         check_on_startup: bool | None = None,
         start_at_login: bool | None = None) -> None:
    """Atomically update settings without discarding other config keys.

    `archive=None` removes the database choice, so the default location
    applies (and may be created on first use). `profile=''` removes the
    login data folder choice; passing None leaves optional settings unchanged."""
    if not providers or not all(isinstance(p, str) for p in providers):
        raise ValueError('select at least one provider')
    if archive is not None and not isinstance(archive, str):
        raise ValueError('archive must be a path')
    if profile is not None and not isinstance(profile, str):
        raise ValueError('profile must be a path')
    if chrome_path is not None and not isinstance(chrome_path, str):
        raise ValueError('chrome_path must be a path')
    _validate_extra(kimi_region=kimi_region, login_timeout=login_timeout)
    root = Path(root)
    data = migrate(_existing_for_update(root / CONFIG_NAME))
    data['providers'] = providers
    _set_or_drop(data, 'archive', archive, keep_empty=True)
    _apply_optional(data, profile=profile, kimi_region=kimi_region,
                    chrome_path=chrome_path, login_timeout=login_timeout,
                    check_on_startup=check_on_startup, start_at_login=start_at_login)
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


def _set_or_drop(data: dict, key: str, value: Any, *, keep_empty: bool = False) -> None:
    if value is None or (value == '' and not keep_empty):
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
