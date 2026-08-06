"""User configuration: an optional `.reclaim.json` at the repo/output root.

Currently one key:

    {"providers": ["googleaistudio", "chatgpt"]}

restricts which providers `reclaim all` runs. Providers not listed are
skipped entirely — no browser window, no login wait. Absent or invalid
file/key -> all providers run (backwards compatible). Single-provider
commands (`reclaim chatgpt`) are unaffected: explicit always wins.
"""

from __future__ import annotations

import json
from pathlib import Path

CONFIG_NAME = '.reclaim.json'

# The single archive root under the repo. All provider output dirs live
# here. In the owner's setup this folder is a symlink to a cloud-synced
# directory (Syncthing), so the chats are available on every device while
# the repo itself stays clean (the folder is gitignored).
ARCHIVE_DIR = 'chats'


def archive_root() -> Path:
    """Repo/<ARCHIVE_DIR> — provider output dirs are subdirectories of it."""
    return Path(__file__).resolve().parents[2] / ARCHIVE_DIR


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
