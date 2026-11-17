"""Find and retire duplicate folders for the same chat (`reclaim tidy`)."""

from __future__ import annotations

import argparse
import contextlib
import json
from dataclasses import dataclass
from pathlib import Path

from . import config, database, writer
from .manifest import SyncState
from .service import offline_session


@dataclass(frozen=True)
class DuplicateCopy:
    provider: str
    chat_id: str
    title: str
    keep_dir: Path
    stale_dir: Path
    has_unique_turns: bool


def _collect_strings(obj, out: set[str]) -> None:
    if isinstance(obj, str):
        stripped = obj.strip()
        if stripped:
            out.add(stripped)
    elif isinstance(obj, list):
        for item in obj:
            _collect_strings(item, out)
    elif isinstance(obj, dict):
        for value in obj.values():
            _collect_strings(value, out)


def _known_texts(keep_dir: Path) -> tuple[set[str], list[str]]:
    payload = writer.read_chat_json(keep_dir) or {}
    turn_texts = {
        str(t.get('text') or '').strip()
        for t in payload.get('turns') or []
        if isinstance(t, dict) and str(t.get('text') or '').strip()
    }
    raw_strings: set[str] = set()
    raw_path = keep_dir / 'raw.json'
    if raw_path.is_file():
        with contextlib.suppress(OSError, ValueError):
            _collect_strings(json.loads(raw_path.read_text(encoding='utf-8')), raw_strings)
    return turn_texts, list(raw_strings)


def _has_unique_turns(stale_dir: Path, keep_turns: set[str], raw_strings: list[str]) -> bool:
    payload = writer.read_chat_json(stale_dir) or {}
    for turn in payload.get('turns') or []:
        if not isinstance(turn, dict):
            continue
        text = str(turn.get('text') or '').strip()
        if not text or text in keep_turns:
            continue
        if any(text in raw_str for raw_str in raw_strings):
            continue
        return True
    return False


def _pick_keep_dir(pdir: Path, provider: str, chat_id: str, dirs: list[Path],
                   sync: SyncState) -> Path:
    recorded = sync.known(chat_id)
    if recorded and isinstance(recorded.get('md'), str):
        rec_dir = writer.locate_recorded(pdir, recorded['md'], chat_id).parent
        for d in dirs:
            if d.resolve() == rec_dir.resolve():
                return d
    return max(dirs, key=lambda d: (d / writer.CHAT_JSON).stat().st_mtime)


def _provider_duplicates(pdir: Path, provider: str) -> list[DuplicateCopy]:
    if not pdir.is_dir():
        return []
    by_id: dict[str, list[Path]] = {}
    for cj in pdir.rglob(writer.CHAT_JSON):
        if not cj.is_file():
            continue
        rel = cj.relative_to(pdir).parts
        if rel and rel[0] in ('_Archived', '_Deleted'):
            continue
        payload = writer.read_chat_json(cj.parent)
        cid = payload.get('id') if payload else None
        if isinstance(cid, str) and cid:
            by_id.setdefault(cid, []).append(cj.parent)
    duplicates: list[DuplicateCopy] = []
    sync = SyncState(pdir, provider, migrate=False)
    for cid, dirs in sorted(by_id.items()):
        if len(dirs) < 2:
            continue
        keep_dir = _pick_keep_dir(pdir, provider, cid, dirs, sync)
        keep_turns, raw_strings = _known_texts(keep_dir)
        title = str((writer.read_chat_json(keep_dir) or {}).get('title') or keep_dir.name)
        for stale_dir in sorted(dirs):
            if stale_dir.resolve() == keep_dir.resolve():
                continue
            duplicates.append(DuplicateCopy(
                provider=provider, chat_id=cid, title=title,
                keep_dir=keep_dir, stale_dir=stale_dir,
                has_unique_turns=_has_unique_turns(stale_dir, keep_turns, raw_strings),
            ))
    return duplicates


def find_duplicates(db: Path) -> list[DuplicateCopy]:
    """Scan all provider folders in `db` for duplicate saved chat folders."""
    db = Path(db)
    found: list[DuplicateCopy] = []
    for provider, dirname in database.PROVIDER_DIRS.items():
        found.extend(_provider_duplicates(db / dirname, provider))
    return found


def retire_duplicates(db: Path, *, include_unique: bool = False) -> list[DuplicateCopy]:
    """Retire stale duplicate folders; keeps copies with unique turns unless requested."""
    db = Path(db)
    retired: list[DuplicateCopy] = []
    for copy in find_duplicates(db):
        if copy.has_unique_turns and not include_unique:
            continue
        pdir = db / database.PROVIDER_DIRS[copy.provider]
        keep_payload = writer.read_chat_json(copy.keep_dir) or {}
        stale_payload = writer.read_chat_json(copy.stale_dir) or {}
        keep_md = copy.keep_dir / str(keep_payload.get('markdown') or f'{copy.keep_dir.name}.md')
        stale_md = copy.stale_dir / str(stale_payload.get('markdown') or f'{copy.stale_dir.name}.md')
        if writer.retire_previous(pdir, copy.chat_id, stale_md, keep_md):
            retired.append(copy)
    return retired


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog='reclaim tidy',
        description='Find duplicate folders for the same chat; pass --apply to retire stale copies.')
    ap.add_argument('--apply', action='store_true',
                    help='retire duplicate folders that have no unique turns')
    ap.add_argument('-o', '--output-dir', default=None,
                    help='database folder (default: the configured one)')
    args = ap.parse_args(argv)
    db = Path(args.output_dir).expanduser().resolve() if args.output_dir else config.archive_root()
    state = database.inspect(db)
    if not state.usable:
        print(f'error: {state.message()}')
        return 2
    copies = find_duplicates(db)
    if not copies:
        print('No duplicate chat folders found.')
        return 0
    for copy in copies:
        note = ' (has unique turns; kept unless resolved manually)' if copy.has_unique_turns else ''
        print(f'  [{copy.provider}] {copy.title}: stale={copy.stale_dir} -> keep={copy.keep_dir}{note}')
    if not args.apply:
        print(f'Found {len(copies)} duplicate folder(s). Re-run with --apply to retire safe duplicates.')
        return 0
    with offline_session(db, 'tidy'):
        retired = retire_duplicates(db)
    print(f'Retired {len(retired)} stale duplicate folder(s).')
    return 0
