"""Provider chat lifecycle planner and safe folder moves (`_Archived`, `_Deleted`)."""

from __future__ import annotations

import contextlib
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from . import database, writer
from .engine import ChatRef, Provider, is_listing_complete
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


def _has_subfolders(folder: Path) -> bool:
    return any(entry.is_dir() for entry in folder.iterdir())


def _move_chat_files(old_dir: Path, target_dir: Path) -> None:
    """Move the files directly inside `old_dir` into `target_dir`; subfolders stay.

    Nothing is moved, and `FileExistsError` is raised, when a file of the same
    name is already in `target_dir`: an existing file is never overwritten.
    `chat.json` moves last, so an interrupted move can be repeated."""
    files = [entry for entry in old_dir.iterdir() if not entry.is_dir()]
    clashes = sorted(entry.name for entry in files if (target_dir / entry.name).exists())
    if clashes:
        raise FileExistsError(
            f'Cannot move the chat folder "{old_dir.name}" into "{target_dir.name}": '
            f'these files already exist there: {", ".join(clashes)}. '
            'Rename or move them, then try again.')
    target_dir.mkdir(parents=True, exist_ok=True)
    for entry in sorted(files, key=lambda path: path.name == writer.CHAT_JSON):
        entry.rename(target_dir / entry.name)
    with contextlib.suppress(OSError):  # not empty: subfolders stay in the old place
        old_dir.rmdir()


def _relocate_chat_folder(old_dir: Path, target_dir: Path) -> None:
    """Move one chat's folder, and nothing that is not part of that chat.

    A folder with only the chat's own files is renamed as a whole. When it
    also holds subfolders, or the target already exists, only the chat's files
    move. Subfolders are other chats (a project named like a chat saves its
    chats inside that chat's folder) or folders the owner added; they stay."""
    if target_dir.exists() or _has_subfolders(old_dir):
        _move_chat_files(old_dir, target_dir)
    else:
        old_dir.rename(target_dir)


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
        _relocate_chat_folder(old_dir, target_dir)
        writer.prune_empty_parents(out_dir, old_dir.parent)
    _update_chat_json_state(target_dir, to_state)
    new_md = target_dir / old_md.name
    sync.mark(chat_id, record.get('updated_at'), str(new_md),
              title=record.get('title'), project=record.get('project'), state=to_state)
    new_rel = target_dir.resolve().relative_to(out_dir.resolve())
    _append_event(out_dir, chat_id, old_rel, new_rel, to_state, reason)
    return target_dir


def _ref_move(ref: ChatRef, prev: dict | None, to_state: str, reason: str) -> PlannedMove | None:
    if prev is None:
        return None
    prev_state = str(prev.get('state') or 'active')
    return PlannedMove(
        ref.id,
        ref.title or str(prev.get('title') or ref.id),
        prev_state,
        to_state,
        reason,
        str(prev.get('md') or ''),
    )


def _collect_ref_actions(refs: list[ChatRef], sync: SyncState,
                         known: dict[str, dict]) -> tuple[list[ChatRef], list[PlannedMove], set[str]]:
    to_download: list[ChatRef] = []
    moves: list[PlannedMove] = []
    moved_ids: set[str] = set()
    for ref in refs:
        prev = known.get(ref.id)
        prev_state = str((prev or {}).get('state') or 'active')
        if ref.state == 'archived':
            if (prev_state == 'active' and ref.id not in moved_ids
                    and (move := _ref_move(ref, prev, 'archived', 'archived at provider'))):
                moves.append(move)
                moved_ids.add(ref.id)
            continue
        status = sync.classify(ref.id, ref.updated_at, title=ref.title or None,
                               project=ref.project, state='active')
        if (status == 'restored' and ref.id not in moved_ids
                and (move := _ref_move(ref, prev, 'active', 'restored at provider'))):
            moves.append(move)
            moved_ids.add(ref.id)
        if status != 'unchanged':
            to_download.append(ref)
    return to_download, moves, moved_ids


def _collect_archived_moves(archived_refs: list[ChatRef], known: dict[str, dict],
                            moved_ids: set[str]) -> tuple[list[PlannedMove], set[str]]:
    moves: list[PlannedMove] = []
    seen_ids: set[str] = set()
    for ref in archived_refs:
        seen_ids.add(ref.id)
        prev = known.get(ref.id)
        if (((prev or {}).get('state') or 'active') == 'active' and ref.id not in moved_ids
                and (move := _ref_move(ref, prev, 'archived', 'archived at provider'))):
            moves.append(move)
            moved_ids.add(ref.id)
    return moves, seen_ids


def _stopped(stop: Callable[[], bool] | None) -> bool:
    return bool(stop is not None and stop())


def _collect_absent_actions(known: dict[str, dict], seen_ids: set[str], moved_ids: set[str],
                            confirm: Callable[[str], str] | None, *,
                            stop: Callable[[], bool] | None = None) -> tuple[list[PlannedMove], list[str]]:
    moves: list[PlannedMove] = []
    unconfirmed: list[str] = []
    for cid, prev in known.items():
        if cid in seen_ids or cid in moved_ids or (prev.get('state') or 'active') != 'active':
            continue
        if _stopped(stop):
            break
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
                   archived_refs: list[ChatRef] | None = None,
                   complete: bool | None = None,
                   confirm: Callable[[str], str] | None = None,
                   stop: Callable[[], bool] | None = None,
                   provider_obj: Provider | None = None,
                   session=None) -> LifecyclePlan:
    """Build a lifecycle plan (downloads + safe moves) without modifying disk."""
    del out_dir  # kept for symmetric API with apply_moves
    known = sync.records()
    is_complete = (complete if complete is not None
                   else is_listing_complete(provider_obj, session)
                   if provider_obj is not None else True)
    to_download, moves, moved_ids = _collect_ref_actions(refs, sync, known)
    archived_moves, archived_seen = _collect_archived_moves(list(archived_refs or ()), known, moved_ids)
    moves.extend(archived_moves)
    seen_ids = {ref.id for ref in refs} | archived_seen
    unconfirmed: list[str] = []
    if is_complete and not _stopped(stop):
        absent_moves, unconfirmed = _collect_absent_actions(known, seen_ids, moved_ids, confirm,
                                                            stop=stop)
        moves.extend(absent_moves)
    total_known = len(known)
    downward = sum(1 for m in moves if m.to_state in ('archived', 'deleted'))
    limit = min(BRAKE_MAX, max(1, int(total_known * BRAKE_SHARE)))
    brake = total_known >= 5 and downward > limit
    reason = (f'Mass-change brake: {downward} of {total_known} chats would move to '
              f'archived/deleted; review required.' if brake else '')
    return LifecyclePlan(
        provider=provider, to_download=to_download, moves=moves,
        unconfirmed=unconfirmed, complete=is_complete,
        brake_triggered=brake, brake_reason=reason,
    )


def apply_moves(out_dir: Path, sync: SyncState, plan: LifecyclePlan, *,
                approved_ids: set[str] | None = None, force: bool = False,
                stop: Callable[[], bool] | None = None,
                applied: list[PlannedMove] | None = None) -> list[Path]:
    """Execute approved moves in `plan`; honors the mass-change brake unless approved.

    Returns the new folders. When `applied` is given, each move that was
    carried out is appended to it, so the caller can report what happened."""
    if _stopped(stop) or (plan.brake_triggered and not force and approved_ids is None):
        return []
    moved: list[Path] = []
    for move in plan.moves:
        if _stopped(stop):
            break
        if approved_ids is not None and move.chat_id not in approved_ids:
            continue
        target = move_chat_folder(out_dir, sync, move.chat_id, move.to_state, reason=move.reason)
        if target is not None:
            moved.append(target)
            if applied is not None:
                applied.append(move)
    return moved


def log_replaced(out_dir: Path, chat_id: str, old_rel: Path, new_rel: Path) -> None:
    """Record in the audit log that an older copy of a chat was replaced by the current one.

    `old_rel` and `new_rel` are folders relative to the provider folder `out_dir`."""
    _append_event(out_dir, chat_id, old_rel, new_rel, 'replaced',
                  'older copy replaced by the current copy')


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


def _remove_chat_files(folder: Path) -> None:
    """Delete the files directly inside a chat folder, then the folder if it is empty.

    These are the chat's Markdown, `chat.json`, `raw.json` and saved media.
    Subfolders are never removed: they hold other chats (a project named like
    a chat saves its chats inside that chat's folder) or folders the owner
    added, and neither was chosen for deletion."""
    for entry in folder.iterdir():
        if not entry.is_dir():
            entry.unlink()
    with contextlib.suppress(OSError):  # not empty: subfolders remain
        folder.rmdir()


def delete_state_chat_permanently(db: Path, entry: StateChatEntry) -> bool:
    """Permanently delete a confirmed `_Archived` or `_Deleted` chat.

    Removes the chat's own files and its folder. Chats and folders inside that
    folder are left untouched."""
    pdir = Path(db) / database.PROVIDER_DIRS[entry.provider]
    if not entry.folder.is_dir() or not writer._inside(entry.folder, pdir):
        return False
    rel = entry.folder.resolve().relative_to(pdir.resolve())
    if not rel.parts or rel.parts[0] not in (ARCHIVED_DIR, DELETED_DIR):
        return False
    _remove_chat_files(entry.folder)
    writer.prune_empty_parents(pdir, entry.folder.parent)
    SyncState(pdir, entry.provider).forget(entry.chat_id)
    _append_event(pdir, entry.chat_id, rel, rel, 'permanently_deleted',
                  'deleted permanently by user')
    return True
