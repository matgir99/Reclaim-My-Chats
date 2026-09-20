"""User configuration: an optional `.reclaim.json` at the repo/output root.

Two optional keys:

    {"providers": ["googleaistudio", "chatgpt"], "archive": "."}

- "providers" restricts which providers `reclaim all` runs. Providers not
  listed are skipped entirely — no browser window, no login wait. Absent
  or invalid file/key -> all providers run (backwards compatible).
  Single-provider commands (`reclaim chatgpt`) are unaffected: explicit
  always wins.
- "archive" overrides where provider output dirs live (see `archive_root`).
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

CONFIG_NAME = '.reclaim.json'

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
    """The parsed config, or {} if absent/invalid."""
    try:
        data = json.loads((Path(root) / CONFIG_NAME).read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save(root: Path, *, providers: list[str], archive: str,
         profile: str | None = None) -> None:
    """Atomically update settings without discarding other config keys."""
    if not providers or not all(isinstance(p, str) for p in providers):
        raise ValueError('select at least one provider')
    if not isinstance(archive, str):
        raise ValueError('archive must be a path')
    root = Path(root)
    path = root / CONFIG_NAME
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError) as exc:
            raise ValueError(f'cannot overwrite invalid {CONFIG_NAME}: {exc}') from exc
        if not isinstance(data, dict):
            raise ValueError(f'cannot overwrite invalid {CONFIG_NAME}')
    else:
        data = {}
    data['providers'] = providers
    data['archive'] = archive
    if profile is not None:
        if not isinstance(profile, str):
            raise ValueError('profile must be a path')
        if profile.strip():
            data['profile'] = profile
        else:
            data.pop('profile', None)
    root.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.reclaim-', suffix='.json', dir=root)
    try:
        if os.name != 'nt':
            os.chmod(tmp, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(data, stream, indent=2, ensure_ascii=False)
            stream.write('\n')
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


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
