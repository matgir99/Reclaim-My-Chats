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
    # One-time renames for providers that changed names (prevents re-downloading
    # the whole archive on the first update run after the rename).
    LEGACY = {'googleaistudio': 'aistudio'}

    def __init__(self, out_dir: Path, provider: str, migrate: bool = True):
        self.path = Path(out_dir) / f'.last_sync_{provider}.json'
        if migrate and not self.path.exists():
            legacy = self.LEGACY.get(provider)
            if legacy:
                old = Path(out_dir) / f'.last_sync_{legacy}.json'
                if old.exists():
                    old.rename(self.path)
        try:
            self._state = json.loads(self.path.read_text())
        except Exception:
            self._state = {}

    def known(self, chat_id: str) -> dict | None:
        return self._state.get(chat_id)

    def classify(self, chat_id: str, updated_at) -> str:
        """'new' (no local record), 'changed' (record, timestamp differs),
        or 'unchanged' (record matches). Missing timestamps fall back to
        presence: an existing record means 'unchanged'."""
        prev = self._state.get(chat_id)
        if prev is None:
            return 'new'
        if updated_at is None:
            return 'unchanged'
        return 'changed' if prev.get('updated_at') != updated_at else 'unchanged'

    def is_unchanged(self, chat_id: str, updated_at) -> bool:
        return self.classify(chat_id, updated_at) == 'unchanged'

    def mark(self, chat_id: str, updated_at, md_path: str):
        self._state[chat_id] = {'updated_at': updated_at, 'md': md_path}

    def save(self):
        self.path.write_text(json.dumps(self._state, indent=2,
                                        ensure_ascii=False))


def plan_fetch(chats: list[dict], updated_map: dict | None, sync: SyncState,
               skip_unchanged: bool) -> dict:
    """Classify each chat against the sync state and count what a run would do.

    Used by --dry-run. Returns:
      fetch / skip              counts of chats that would be fetched / skipped
      new / changed / unchanged classification counts (against sync state)
      titles                    titles of the chats that would be fetched
    """
    updated_map = updated_map or {}
    n_new = n_changed = n_unchanged = 0
    titles: list[str] = []
    for c in chats:
        st = sync.classify(c['id'], updated_map.get(c['id']))
        if st == 'new':
            n_new += 1
        elif st == 'changed':
            n_changed += 1
        else:
            n_unchanged += 1
        if not skip_unchanged or st != 'unchanged':
            titles.append(str(c.get('title') or c.get('name') or c.get('id')
                              or '?'))
    if skip_unchanged:
        return {'fetch': n_new + n_changed, 'skip': n_unchanged,
                'new': n_new, 'changed': n_changed,
                'unchanged': n_unchanged, 'titles': titles}
    return {'fetch': len(chats), 'skip': 0, 'new': n_new,
            'changed': n_changed, 'unchanged': n_unchanged, 'titles': titles}


def print_dry_run(chats: list[dict], updated_map: dict | None, sync: SyncState,
                  skip_unchanged: bool, log: bool = False) -> int:
    """Print what a run WOULD do (--dry-run). Returns the exit code (0).

    Default (quiet): the counts line plus the titles that would be fetched.
    log=True: per-chat lines in the real run's [i/N] format, then the
    counts line. Nothing is downloaded or written."""
    updated_map = updated_map or {}
    plan = plan_fetch(chats, updated_map, sync, skip_unchanged)
    if log:
        for i, c in enumerate(chats, 1):
            st = sync.classify(c['id'], updated_map.get(c['id']))
            label = (c.get('title') or c.get('name') or c.get('id') or '?')[:55]
            if not skip_unchanged:
                action = 'fetch (fresh)'
            elif st == 'unchanged':
                action = 'skip (unchanged)'
            elif st == 'new':
                action = 'fetch (new)'
            else:
                action = 'fetch (changed)'
            print(f'[{i}/{len(chats)}] {label} -> {action}')
    if skip_unchanged:
        print(f'would fetch: {plan["fetch"]} ({plan["new"]} new, '
              f'{plan["changed"]} changed) · would skip: '
              f'{plan["skip"]} unchanged')
    else:
        print(f'would fetch: {plan["fetch"]} (fresh) · would skip: 0')
    if not log:
        if plan['titles']:
            for t in plan['titles']:
                print(f'  - {t}')
        else:
            print('  (nothing to fetch)')
    return 0
