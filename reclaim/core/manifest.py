"""Run manifests and incremental-sync bookkeeping.

Manifest: one JSON per run at <out_dir>/.reclaim_manifest.json — totals and
per-chat stats for regression comparison.

Sync state: one JSON per provider at <out_dir>/.last_sync_<provider>.json —
maps chat id -> {'updated_at': str|None, 'md': str}. Providers that expose
update timestamps can skip unchanged chats; others fall back to
"skip if already saved".

The sync state is saved after every chat and written atomically, so an
interrupted run keeps the records of the chats it saved. It is a cache of
what the chat folders already say: each chat.json stores the provider
timestamp, so a missing or damaged sync file is rebuilt from the folders.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from .events import EventSink, emit
from .fsutil import atomic_write_text
from .model import Chat
from .writer import (CHAT_JSON, locate_recorded, read_chat_json, retire_previous,
                     write_chat, write_raw)


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
    atomic_write_text(path, json.dumps(manifest, indent=2, ensure_ascii=False, default=str))
    return path


def write_attempt(out_dir: Path, started: float, code: int, error: str | None) -> None:
    """Keep operation failures separate from actual per-chat archive statistics."""
    out_dir.mkdir(parents=True, exist_ok=True)
    data = {'started': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(started)),
            'exit_code': code, 'error': error}
    atomic_write_text(out_dir / '.reclaim_last_attempt.json', json.dumps(data, ensure_ascii=False))


def rebuild_records(out_dir: Path) -> dict:
    """Sync records reconstructed from the chat folders under `out_dir`.

    Uses each chat.json's id, provider timestamp and Markdown name. A chat
    saved in two folders keeps the most recently written one. Chats saved
    before chat.json recorded the timestamp get `updated_at: None`, so the
    next update downloads them once more."""
    out_dir = Path(out_dir)
    records: dict[str, dict] = {}
    newest: dict[str, float] = {}
    if not out_dir.is_dir():
        return records
    for path in out_dir.rglob(CHAT_JSON):
        payload = read_chat_json(path.parent)
        cid = payload.get('id') if payload else None
        if not isinstance(cid, str) or not cid:
            continue
        written = path.stat().st_mtime
        if newest.get(cid, -1.0) >= written:
            continue
        newest[cid] = written
        md = path.parent / str((payload or {}).get('markdown') or f'{path.parent.name}.md')
        records[cid] = {'updated_at': (payload or {}).get('provider_updated_at'),
                        'md': md.relative_to(out_dir).as_posix()}
    return records


class SyncState:
    # One-time renames for providers that changed names (prevents re-downloading
    # the whole archive on the first update run after the rename).
    LEGACY = {'googleaistudio': 'aistudio'}

    def __init__(self, out_dir: Path, provider: str, migrate: bool = True):
        """`migrate=False` opens the records read-only, for previews."""
        self.path = Path(out_dir) / f'.last_sync_{provider}.json'
        self.provider = provider
        self.readonly = not migrate
        if migrate and not self.path.exists():
            legacy = self.LEGACY.get(provider)
            if legacy:
                old = Path(out_dir) / f'.last_sync_{legacy}.json'
                if old.exists():
                    old.rename(self.path)
        self._state = self._load()

    def _load(self) -> dict:
        try:
            text = self.path.read_text(encoding='utf-8')
        except FileNotFoundError:
            return self._rebuild('is missing', move_aside=False)
        except (OSError, UnicodeDecodeError) as exc:
            return self._rebuild(f'cannot be read ({exc})', move_aside=True)
        try:
            data = json.loads(text)
        except ValueError:
            return self._rebuild('is damaged (not valid JSON)', move_aside=True)
        if not isinstance(data, dict) or not all(isinstance(v, dict) for v in data.values()):
            return self._rebuild('has an unexpected format', move_aside=True)
        return data

    def _rebuild(self, problem: str, *, move_aside: bool) -> dict:
        records = rebuild_records(self.path.parent)
        if not records and not move_aside:
            return records  # a provider folder without saved chats yet
        where = ''
        if move_aside and not self.readonly:
            backup = self.path.with_name(
                f'{self.path.name}.damaged-{time.strftime("%Y%m%d-%H%M%S")}')
            os.replace(self.path, backup)
            where = f'; the old file was kept as {backup.name}'
        print(f'warning: the sync record {self.path.name} {problem}{where}. '
              f'Rebuilt {len(records)} records from the chat folders.')
        if not self.readonly:
            self._state = records
            self.save()
        return records

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

    def _resolve(self, md: str) -> Path:
        return locate_recorded(self.path.parent, md)

    def mark(self, chat_id: str, updated_at, md_path: str):
        """Record a written chat and save at once; retire its previous
        folder if it moved. Read-only states only update memory."""
        out_dir = self.path.parent
        current = Path(md_path)
        previous = (self._state.get(chat_id) or {}).get('md')
        if not self.readonly and isinstance(previous, str) and previous:
            try:
                retire_previous(out_dir, chat_id, self._resolve(previous), current)
            except OSError as exc:
                print(f'  note: could not remove previous folder of {chat_id}: {exc}')
        try:
            stored = current.resolve().relative_to(out_dir.resolve()).as_posix()
        except ValueError:
            stored = str(current)
        self._state[chat_id] = {'updated_at': updated_at, 'md': stored}
        self.save()

    def save(self):
        if self.readonly:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(self.path, json.dumps(self._state, indent=2, ensure_ascii=False,
                                                default=str))


def save_chat(chat: Chat, out_dir: Path, sync: SyncState, sync_id: str, updated_at,
              raw=None, *, sink: EventSink | None = None, provider: str = '') -> dict:
    """Save one downloaded chat: its folder, raw.json and sync record.

    Shared by every provider. A raw.json that cannot be written is reported
    (printed, logged and sent to the window) instead of ignored; the chat
    itself still counts as saved. Returns write_chat's stats plus
    'warnings'."""
    stats = write_chat(chat, out_dir, updated_at=updated_at)
    warnings: list[str] = []
    if raw is not None:
        try:
            write_raw(stats['dir'], raw)
        except Exception as exc:  # noqa: BLE001 - reported, never hidden
            warnings.append(f'raw.json was not saved: {exc}')
    for warning in warnings:
        print(f'  warning: {chat.title[:55]}: {warning}')
        emit(sink, 'warning', provider, chat.title, message=warning)
    sync.mark(sync_id, updated_at, str(stats['md']))
    stats['warnings'] = warnings
    return stats


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
                  skip_unchanged: bool, log: bool = False,
                  sink: EventSink | None = None, provider: str = '') -> int:
    """Print what a run WOULD do (--dry-run). Returns the exit code (0).

    Default (quiet): the counts line plus the titles that would be fetched.
    log=True: per-chat lines in the real run's [i/N] format, then the
    counts line. Nothing is downloaded or written."""
    updated_map = updated_map or {}
    plan = plan_fetch(chats, updated_map, sync, skip_unchanged)
    if sink is not None:
        for i, chat in enumerate(chats, 1):
            state = sync.classify(chat['id'], updated_map.get(chat['id']))
            action = 'skip' if skip_unchanged and state == 'unchanged' else 'fetch'
            emit(sink, 'chat_planned', provider,
                 str(chat.get('title') or chat.get('name') or chat.get('id') or '?'),
                 i, len(chats), f'{action} ({state})')
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
