"""Run manifests and incremental-sync bookkeeping.

Manifest: one JSON per run at <out_dir>/.reclaim_manifest.json — totals and
per-chat stats for regression comparison.

Sync state: one JSON per provider at <out_dir>/.last_sync_<provider>.json —
maps chat id -> {'updated_at': str|None, 'md': str}. Providers that expose
update timestamps can skip unchanged chats; others fall back to
"skip if already saved".
"""

from __future__ import annotations

import json
import time
from pathlib import Path


def write_manifest(out_dir: Path, provider: str, results: list[dict],
                   started: float) -> Path:
    out_dir = Path(out_dir)
    manifest = {
        'provider': provider,
        'started': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(started)),
        'duration_s': round(time.time() - started, 1),
        'totals': {
            'chats': len(results),
            'ok': sum(1 for r in results if r.get('ok')),
            'failed': sum(1 for r in results if not r.get('ok')),
            'images': sum(r.get('images', 0) for r in results),
            'docs': sum(r.get('docs', 0) for r in results),
            'chars': sum(r.get('chars', 0) for r in results),
        },
        'chats': results,
    }
    path = out_dir / '.reclaim_manifest.json'
    path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False))
    return path


class SyncState:
    def __init__(self, out_dir: Path, provider: str):
        self.path = Path(out_dir) / f'.last_sync_{provider}.json'
        try:
            self._state = json.loads(self.path.read_text())
        except Exception:
            self._state = {}

    def known(self, chat_id: str) -> dict | None:
        return self._state.get(chat_id)

    def is_unchanged(self, chat_id: str, updated_at) -> bool:
        prev = self._state.get(chat_id)
        if prev is None:
            return False
        if updated_at is None:
            return True  # no timestamp info -> treat "already saved" as unchanged
        return prev.get('updated_at') == updated_at

    def mark(self, chat_id: str, updated_at, md_path: str):
        self._state[chat_id] = {'updated_at': updated_at, 'md': md_path}

    def save(self):
        self.path.write_text(json.dumps(self._state, indent=2,
                                        ensure_ascii=False))
