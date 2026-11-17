"""Provider chat lifecycle planner and safe folder moves (`_Archived`, `_Deleted`)."""

from __future__ import annotations

import json
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from . import database, writer
from .engine import ChatRef
from .fsutil import atomic_write_text
from .manifest import SyncState

ARCHIVED_DIR = '_Archived'
DELETED_DIR = '_Deleted'
EVENTS_LOG = '.reclaim_events.jsonl'
BRAKE_SHARE = 0.10
BRAKE_MAX = 20


@dataclass(frozen=True)
class PlannedMove:
    chat_id: str
    title: str
    from_state: str
    to_state: str
    reason: str
    md: str = ''


@dataclass
class LifecyclePlan:
    provider: str
    to_download: list[ChatRef] = field(default_factory=list)
    moves: list[PlannedMove] = field(default_factory=list)
    unconfirmed: list[str] = field(default_factory=list)
    complete: bool = True
    brake_triggered: bool = False
    brake_reason: str = ''


def _active_relative_dir(out_dir: Path, old_dir: Path) -> Path:
    rel = old_dir.resolve().relative_to(out_dir.resolve())
    parts = rel.parts
    if parts and parts[0] in (ARCHIVED_DIR, DELETED_DIR):
        rest = parts[1:]
        return Path(*rest) if rest else Path(old_dir.name)
    return rel


def _update_chat_json_state(chat_dir: Path, to_state: str) -> None:
    path = chat_dir / writer.CHAT_JSON
    payload = writer.read_chat_json(chat_dir)
    if payload is None:
        return
    payload['state'] = to_state
    atomic_write_text(path, json.dumps(payload, indent=2, ensure_ascii=False, default=str))


def _prune_empty_parents(out_dir: Path, parent: Path) -> None:
    while writer._inside(parent, out_dir) and not any(parent.iterdir()):
        parent.rmdir()
        parent = parent.parent


def _append_event(out_dir: Path, chat_id: str, old_rel: Path, new_rel: Path,
                  to_state: str, reason: str) -> None:
    entry = {
        'time': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'chat_id': chat_id,
        'from': old_rel.as_posix(),
        'to': new_rel.as_posix(),
        'state': to_state,
        'reason': reason,
    }
    with open(out_dir / EVENTS_LOG, 'a', encoding='utf-8') as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + '\n')


def move_chat_folder(out_dir: Path, sync: SyncState, chat_id: str, to_state: str,
                     *, reason: str = '') -> Path | None:
    """Move one saved chat folder between active, `_Archived`, and `_Deleted`."""
    if to_state not in ('active', 'archived', 'deleted'):
        raise ValueError(f'unknown target state: {to_state}')
    record = sync.known(chat_id)
    if record is None or not isinstance(record.get('md'), str):
        return None
    out_dir = Path(out_dir)
    old_md = writer.locate_recorded(out_dir, record['md'], chat_id)
    old_dir = old_md.parent
    if not old_dir.is_dir() or not writer._inside(old_dir, out_dir):
        return None
    old_rel = old_dir.resolve().relative_to(out_dir.resolve())
    active_rel = _active_relative_dir(out_dir, old_dir)
    prefix = {'active': (), 'archived': (ARCHIVED_DIR,), 'deleted': (DELETED_DIR,)}[to_state]
    target_dir = out_dir.joinpath(*prefix, active_rel)
    if old_dir.resolve() != target_dir.resolve():
        target_dir.parent.mkdir(parents=True, exist_ok=True)
        if target_dir.exists():
            target_dir = writer._chat_folder(target_dir.parent, target_dir.name, chat_id)
        old_dir.rename(target_dir)
        _prune_empty_parents(out_dir, old_dir.parent)
    _update_chat_json_state(target_dir, to_state)
    new_md = target_dir / old_md.name
    sync.mark(chat_id, record.get('updated_at'), str(new_md),
              title=record.get('title'), project=record.get('project'), state=to_state)
    new_rel = target_dir.resolve().relative_to(out_dir.resolve())
    _append_event(out_dir, chat_id, old_rel, new_rel, to_state, reason)
    return target_dir


def _collect_ref_actions(refs: list[ChatRef], sync: SyncState,
                         known: dict[str, dict]) -> tuple[list[ChatRef], list[PlannedMove], set[str]]:
    to_download: list[ChatRef] = []
    moves: list[PlannedMove] = []
    seen_ids: set[str] = set()
    moved_ids: set[str] = set()
    for ref in refs:
        seen_ids.add(ref.id)
        prev = known.get(ref.id)
        prev_state = str((prev or {}).get('state') or 'active')
        if ref.state == 'archived':
            if prev is not None and prev_state == 'active' and ref.id not in moved_ids:
                moves.append(PlannedMove(ref.id, ref.title or str(prev.get('title') or ref.id),
                                         'active', 'archived', 'archived at provider',
                                         str(prev.get('md') or '')))
                moved_ids.add(ref.id)
            continue
        status = sync.classify(ref.id, ref.updated_at, title=ref.title or None,
                               project=ref.project, state='active')
        if status == 'restored' and prev is not None and ref.id not in moved_ids:
            moves.append(PlannedMove(ref.id, ref.title or str(prev.get('title') or ref.id),
                                     prev_state, 'active', 'restored at provider',
                                     str(prev.get('md') or '')))
            moved_ids.add(ref.id)
        if status != 'unchanged':
            to_download.append(ref)
    return to_download, moves, moved_ids


def _collect_absent_actions(known: dict[str, dict], seen_ids: set[str], moved_ids: set[str],
                            confirm: Callable[[str], str] | None) -> tuple[list[PlannedMove], list[str]]:
    moves: list[PlannedMove] = []
    unconfirmed: list[str] = []
    for cid, prev in known.items():
        if cid in seen_ids or cid in moved_ids:
            continue
        if (prev.get('state') or 'active') != 'active':
            continue
        verdict = confirm(cid) if confirm is not None else 'unconfirmed'
        if verdict in ('deleted', 'archived'):
            reason = ('confirmed missing at provider' if verdict == 'deleted'
                      else 'confirmed archived at provider')
            moves.append(PlannedMove(cid, str(prev.get('title') or cid), 'active',
                                     verdict, reason, str(prev.get('md') or '')))
        else:
            unconfirmed.append(cid)
    return moves, unconfirmed


def plan_lifecycle(out_dir: Path, provider: str, refs: list[ChatRef], sync: SyncState, *,
                   archived_refs: list[ChatRef] | None = None, complete: bool = True,
                   confirm: Callable[[str], str] | None = None) -> LifecyclePlan:
    """Build a lifecycle plan (downloads + safe moves) without modifying disk."""
    del out_dir  # kept for symmetric API with apply_moves
    known = sync.records()
    to_download, moves, moved_ids = _collect_ref_actions(refs, sync, known)
    seen_ids = {ref.id for ref in refs}
    for ref in archived_refs or ():
        seen_ids.add(ref.id)
        prev = known.get(ref.id)
        if prev is not None and (prev.get('state') or 'active') == 'active' and ref.id not in moved_ids:
            moves.append(PlannedMove(ref.id, ref.title or str(prev.get('title') or ref.id),
                                     'active', 'archived', 'archived at provider',
                                     str(prev.get('md') or '')))
            moved_ids.add(ref.id)
    unconfirmed: list[str] = []
    if complete:
        absent_moves, unconfirmed = _collect_absent_actions(known, seen_ids, moved_ids, confirm)
        moves.extend(absent_moves)
    total_known = len(known)
    downward = sum(1 for m in moves if m.to_state in ('archived', 'deleted'))
    limit = min(BRAKE_MAX, max(1, int(total_known * BRAKE_SHARE)))
    brake = total_known >= 5 and downward > limit
    reason = (f'Mass-change brake: {downward} of {total_known} chats would move to '
              f'archived/deleted; review required.' if brake else '')
    return LifecyclePlan(
        provider=provider, to_download=to_download, moves=moves,
        unconfirmed=unconfirmed, complete=complete,
        brake_triggered=brake, brake_reason=reason,
    )


def apply_moves(out_dir: Path, sync: SyncState, plan: LifecyclePlan, *,
                approved_ids: set[str] | None = None, force: bool = False) -> list[Path]:
    """Execute approved moves in `plan`; honors the mass-change brake unless approved."""
    if plan.brake_triggered and not force and approved_ids is None:
        return []
    moved: list[Path] = []
    for move in plan.moves:
        if approved_ids is not None and move.chat_id not in approved_ids:
            continue
        target = move_chat_folder(out_dir, sync, move.chat_id, move.to_state, reason=move.reason)
        if target is not None:
            moved.append(target)
    return moved


@dataclass(frozen=True)
class StateChatEntry:
    provider: str
    chat_id: str
    title: str
    state: str
    folder: Path
    md: Path


def list_state_chats(db: Path) -> list[StateChatEntry]:
    """List every saved chat under `_Archived` or `_Deleted` across providers."""
    db = Path(db)
    entries: list[StateChatEntry] = []
    for provider, dirname in database.PROVIDER_DIRS.items():
        pdir = db / dirname
        if not pdir.is_dir():
            continue
        for state, subdir in (('archived', ARCHIVED_DIR), ('deleted', DELETED_DIR)):
            root = pdir / subdir
            if not root.is_dir():
                continue
            for cj in sorted(root.rglob(writer.CHAT_JSON)):
                if not cj.is_file():
                    continue
                payload = writer.read_chat_json(cj.parent) or {}
                cid = str(payload.get('id') or cj.parent.name)
                title = str(payload.get('title') or cj.parent.name)
                md_name = str(payload.get('markdown') or f'{cj.parent.name}.md')
                entries.append(StateChatEntry(
                    provider=provider, chat_id=cid, title=title, state=state,
                    folder=cj.parent, md=cj.parent / md_name,
                ))
    return entries


def restore_state_chat(db: Path, entry: StateChatEntry) -> Path | None:
    """Restore an `_Archived` or `_Deleted` chat back into active folders."""
    pdir = Path(db) / database.PROVIDER_DIRS[entry.provider]
    sync = SyncState(pdir, entry.provider)
    if sync.known(entry.chat_id) is None:
        payload = writer.read_chat_json(entry.folder) or {}
        sync.mark(entry.chat_id, payload.get('provider_updated_at'), str(entry.md),
                  title=entry.title, project=payload.get('project'), state=entry.state)
    return move_chat_folder(pdir, sync, entry.chat_id, 'active', reason='restored by user')


def delete_state_chat_permanently(db: Path, entry: StateChatEntry) -> bool:
    """Permanently delete a confirmed `_Archived` or `_Deleted` chat folder."""
    pdir = Path(db) / database.PROVIDER_DIRS[entry.provider]
    if not entry.folder.is_dir() or not writer._inside(entry.folder, pdir):
        return False
    rel = entry.folder.resolve().relative_to(pdir.resolve())
    if not rel.parts or rel.parts[0] not in (ARCHIVED_DIR, DELETED_DIR):
        return False
    shutil.rmtree(entry.folder)
    _prune_empty_parents(pdir, entry.folder.parent)
    SyncState(pdir, entry.provider).forget(entry.chat_id)
    _append_event(pdir, entry.chat_id, rel, rel, 'permanently_deleted',
                  'deleted permanently by user')
    return True
