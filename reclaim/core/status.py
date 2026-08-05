"""`reclaim status` — offline archive overview.

Reads only local files: each provider directory's latest run manifest
(.reclaim_manifest.json) and sync state (.last_sync_<provider>.json).
Never launches the browser and never touches the network.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from pathlib import Path

from .manifest import SyncState

REPO_ROOT = Path(__file__).resolve().parents[2]

# (provider name, output dir name) in display order
PROVIDER_DIRS = [
    ('googleaistudio', 'Google AI Studio'),
    ('deepseek', 'Deepseek Chat'),
    ('kimi', 'Kimi Chat'),
    ('chatgpt', 'ChatGPT'),
]


@dataclass
class ProviderStatus:
    provider: str
    dir: str
    archived: bool = False
    chat_folders: int = 0          # chat dirs with a chat.json on disk
    synced_chats: int = 0          # distinct chats in the sync record
    last_sync: str | None = None   # manifest 'started' timestamp
    duration_s: float | None = None
    last_run: dict[str, int] = field(default_factory=dict)


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _count_chat_folders(pdir: Path) -> int:
    return sum(1 for p in pdir.rglob('chat.json') if p.is_file())


def describe(provider: str, dirname: str, root: Path) -> ProviderStatus:
    """One provider's status from local files only."""
    pdir = Path(root) / dirname
    info = ProviderStatus(provider=provider, dir=str(pdir),
                          archived=pdir.is_dir())
    if not info.archived:
        return info
    manifest = _read_json(pdir / '.reclaim_manifest.json')
    if manifest:
        info.last_sync = manifest.get('started')
        info.duration_s = manifest.get('duration_s')
        totals = manifest.get('totals') or {}
        info.last_run = {
            'chats': totals.get('chats', 0),
            'ok': totals.get('ok', 0),
            'failed': totals.get('failed', 0),
            'images': totals.get('images', 0),
            'docs': totals.get('docs', 0),
            'chars': totals.get('chars', 0),
        }
    sync_path = pdir / f'.last_sync_{provider}.json'
    if not sync_path.exists():
        legacy = SyncState.LEGACY.get(provider)
        if legacy:
            alt = pdir / f'.last_sync_{legacy}.json'
            if alt.exists():
                sync_path = alt  # pre-rename archive; migration happens on run
    sync = _read_json(sync_path)
    info.synced_chats = len(sync)
    info.chat_folders = _count_chat_folders(pdir)
    return info


def scan(root: Path) -> list[ProviderStatus]:
    """Status for every provider, in fixed display order."""
    return [describe(prov, d, root) for prov, d in PROVIDER_DIRS]


def format_status(info: ProviderStatus) -> str:
    """Human-readable block for one provider."""
    if not info.archived:
        return f'{info.dir} — not archived yet'
    lines = [info.dir]
    lines.append(f'  chats archived: {info.chat_folders} '
                 f'(sync records: {info.synced_chats})')
    if info.last_run:
        r = info.last_run
        lines.append(f'  last run: {r["ok"]} ok, {r["failed"]} failed '
                     f'({r["chats"]} processed)')
        lines.append(f'  last sync: {info.last_sync} · {info.duration_s}s')
        img = f'{r["images"]} image' if r['images'] == 1 else f'{r["images"]} images'
        doc = f'{r["docs"]} doc' if r['docs'] == 1 else f'{r["docs"]} docs'
        lines.append(f'  totals: {img} · {doc} · {r["chars"]:,} chars')
    else:
        lines.append('  last run: (no manifest yet)')
    return '\n'.join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(prog='reclaim status')
    ap.add_argument('-o', '--output-dir', default=str(REPO_ROOT),
                    help='Scan root (default: repo root)')
    args = ap.parse_args(argv)
    root = Path(args.output_dir)
    print(f'ReclaimMyChats archive status — scan root: {root}\n')
    for info in scan(root):
        print(format_status(info))
        print()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
