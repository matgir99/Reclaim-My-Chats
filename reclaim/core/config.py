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
from pathlib import Path

CONFIG_NAME = '.reclaim.json'

# The single archive root under the repo. All provider output dirs live
# here. In the owner's setup this folder is a symlink to a cloud-synced
# directory (Syncthing), so the chats are available on every device while
# the repo itself stays clean (the folder is gitignored).
ARCHIVE_DIR = 'chats'


def archive_root(root: Path | None = None) -> Path:
    """Where provider output dirs live.

    Default: <repo>/<ARCHIVE_DIR>. Override once per machine with the
    "archive" key in .reclaim.json — an absolute path is used as-is, a
    relative path is resolved against the repo root, and "" or "." keeps
    the pre-chats layout (provider dirs directly at the repo root). Any
    of these may be a symlink; the code never looks at the target, so a
    cloud-synced archive works for everyone.
    """
    base = Path(root) if root is not None else Path(__file__).resolve().parents[2]
    override = load(base).get('archive')
    if isinstance(override, str) and override.strip():
        p = Path(override).expanduser()
        joined = p if p.is_absolute() else base / p
        return Path(os.path.normpath(joined))  # normalize '.' / '..' segments
    if override == '':
        return base  # explicit empty string: provider dirs at the root itself
    return base / ARCHIVE_DIR


def load(root: Path) -> dict:
    """The parsed config, or {} if absent/invalid."""
    try:
        data = json.loads((Path(root) / CONFIG_NAME).read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


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
