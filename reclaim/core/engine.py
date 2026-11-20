"""The provider engine: everything the providers do the same way.

A provider knows only its service: how to log in, list its chats, turn a
chat URL into a reference and fetch one chat (see `Provider`). The engine
does the rest identically for all of them: command-line options, the title,
`--skip` and `--limit` filters, `--list` and `--dry-run`, skipping unchanged
chats, saving each chat (folder, raw.json, sync record), progress events
(never printed output: see events.py), the run manifest and the exit code. A
fix made here reaches every provider.
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
import traceback
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import config
from .events import EventSink, emit, resolve, routed
from .manifest import KEEP, SyncState, finish_run, report_dry_run, save_chat, stats_text
from .model import Chat

if TYPE_CHECKING:  # lifecycle imports this module, so only the type is imported here
    from .lifecycle import PlannedMove

_stop = threading.local()


@contextmanager
def stoppable(event: threading.Event) -> Iterator[threading.Event]:
    """Allow `event.set()` from another thread to stop a run after the current chat."""
    previous = getattr(_stop, 'event', None)
    _stop.event = event
    try:
        yield event
    finally:
        _stop.event = previous


def should_stop(stop: threading.Event | None = None) -> bool:
    """True when the active run (or explicit stop event) has been asked to stop."""
    active = stop if stop is not None else getattr(_stop, 'event', None)
    return bool(active is not None and active.is_set())


@dataclass
class ChatRef:
    """One chat as the provider lists it."""
    id: str                # the id its sync record uses
    title: str = ''
    updated_at: Any = None  # the provider's own timestamp, if it has one
    data: Any = None       # the provider's listing entry, handed back to fetch()
    project: str | None = None
    state: str = 'active'
    listed: bool = True    # False for a chat taken from a URL: its project is not known

    def listed_project(self):
        """The project for comparing with the sync record: the listing's (None
        when the chat is in no project), or KEEP for a chat taken from a URL."""
        return self.project if self.listed else KEEP


def is_listing_complete(provider: Provider, session) -> bool:
    """Whether `provider.listing(session)` covers every chat on the service."""
    return bool(getattr(session, 'listing_complete', getattr(provider, 'listing_complete', True)))


@dataclass
class Fetched:
    """One downloaded chat."""
    chat: Chat
    raw: Any = None        # saved as raw.json unless --no-raw
    detail: str = ''       # shown under the chat with --log, e.g. 'images: 2'
    extra: dict = field(default_factory=dict)  # added to the chat's manifest entry


class Provider:
    """What a provider supplies. Subclasses set the attributes and implement
    `open`, `listing`, `from_url` and `fetch`."""

    name = ''              # command name and sync-record name: reclaim <name>
    folder = ''            # the provider's folder inside the database
    description = ''       # first line of `reclaim <name> --help`
    noun = 'chats'         # what the service calls its chats, in messages
    pace = 0.3             # pause between two downloads, in seconds

    def open(self, page, /) -> Any:
        """Log in if needed; return the session handed to the other methods."""
        raise NotImplementedError

    def listing(self, session, /) -> list[ChatRef]:
        raise NotImplementedError

    def from_url(self, session, url: str, /) -> ChatRef:
        raise NotImplementedError

    def fetch(self, session, ref: ChatRef, /) -> Fetched:
        raise NotImplementedError

    def listing_text(self) -> str:
        return f'Listing {self.noun}...'

    def found_text(self, refs: list[ChatRef], /) -> str:
        return f'Found {len(refs)} {self.noun}'


def build_parser(provider: Provider) -> argparse.ArgumentParser:
    """The options every provider command accepts (`reclaim <provider> --help`)."""
    ap = argparse.ArgumentParser(prog=f'reclaim {provider.name}',
                                 description=provider.description)
    ap.add_argument('title', nargs='?',
                    help='fetch chats whose title contains this (case-insensitive)')
    ap.add_argument('--rebuild', action='store_true',
                    help='fetch everything, overwriting local copies')
    ap.add_argument('--url', help='fetch one exact chat URL')
    ap.add_argument('--list', action='store_true',
                    help='print chat titles, no download')
    ap.add_argument('--log', action='store_true',
                    help='full per-chat log + verbose progress and timings')
    ap.add_argument('--dry-run', action='store_true',
                    help='preview what would be fetched; nothing downloaded')
    ap.add_argument('--skip', type=int, default=0,
                    help='skip the first N chats in the listing')
    ap.add_argument('--limit', type=int, default=0,
                    help='fetch at most N chats')
    ap.add_argument('--no-raw', action='store_true',
                    help='do not save media-stripped raw.json per chat')
    ap.add_argument('--only-id', action='append', dest='only_ids',
                    help='fetch only this chat id (repeatable)')
    ap.add_argument('-o', '--output-dir',
                    default=str(config.archive_root() / provider.folder))
    return ap


def parse_args(provider: Provider, argv=None) -> argparse.Namespace:
    """Parse and check options (shared by every command and the window)."""
    ap = build_parser(provider)
    args = ap.parse_args(argv)
    if args.title and args.url:
        ap.error('pass a title or --url, not both')
    return args


def select(refs: list[ChatRef], args) -> list[ChatRef]:
    """Apply the title filter, then --skip and --limit."""
    if args.title:
        refs = [r for r in refs if args.title.lower() in (r.title or '').lower()]
    if args.skip:
        refs = refs[args.skip:]
    if args.limit:
        refs = refs[:args.limit]
    return refs


def run_session(provider: Provider, page, args, sink: EventSink | None = None) -> int:
    """One provider on an already opened browser page. Returns the exit code:
    0 all saved, 1 nothing matched, 2 some chats failed. Events go to `sink`,
    else to the run's route, else to the terminal (verbose with --log)."""
    sink = resolve(sink, verbose=args.log)
    with routed(sink):  # the provider's own events (login, warnings) too
        return _session(provider, page, args, sink)


def _confirm_callback(provider: Provider, session) -> Callable[[str], str] | None:
    confirm_api = getattr(provider, 'confirm_status', None)
    if not callable(confirm_api):
        return None
    return lambda cid: str(confirm_api(session, cid))


def _move_summary(moves) -> tuple[str, str]:
    counts: dict[str, int] = {}
    for move in moves:
        counts[move.to_state] = counts.get(move.to_state, 0) + 1
    parts = ', '.join(f'{n} {state}' for state, n in sorted(counts.items()))
    titles = '\n'.join(f'  - {m.title} ({m.to_state})' for m in moves)
    return f'would move: {len(moves)} ({parts})', titles


def _emit_and_apply_lifecycle(plan, out_dir: Path, sync: SyncState, args,
                              provider: Provider, sink: EventSink, *, dry_run: bool) -> None:
    from . import lifecycle
    only = getattr(args, 'only_ids', None)
    approved = set(only) if only else None
    if dry_run:
        for idx, move in enumerate(plan.moves, 1):
            emit(sink, 'chat_planned', provider.name, move.title,
                 idx, len(plan.moves), f'move ({move.to_state})', detail=move.chat_id)
        if plan.moves:
            msg, detail = _move_summary(plan.moves)
            emit(sink, 'plan', provider.name, total=len(plan.moves), message=msg, detail=detail)
    # Explicit --only-id (e.g. GUI Review confirmation) approves braked moves;
    # unfiltered updates still stop and warn.
    if plan.brake_triggered and (dry_run or not approved):
        emit(sink, 'warning', provider.name, message=plan.brake_reason)
    if not dry_run:
        applied: list[PlannedMove] = []
        lifecycle.apply_moves(out_dir, sync, plan, approved_ids=approved, stop=should_stop,
                              applied=applied)
        _report_moves(sink, provider.name, applied)


def _plural(count: int, one: str, many: str) -> str:
    return f'{count} {one if count == 1 else many}'


def _report_moves(sink: EventSink, provider: str, applied: list[PlannedMove]) -> None:
    """Say what a run filed away or restored; the moves themselves print nothing."""
    if not applied:
        return
    counts = Counter(move.to_state for move in applied)
    filed = counts['archived'] + counts['deleted']
    pieces = []
    if filed:
        parts = ', '.join(f'{counts[state]} as {state}' for state in ('archived', 'deleted')
                          if counts[state])
        pieces.append(f'filed {_plural(filed, "chat", "chats")} ({parts})')
    if counts['active']:
        pieces.append(f'restored {_plural(counts["active"], "chat", "chats")}')
    verbs = {'archived': 'archived', 'deleted': 'deleted', 'active': 'restored'}
    titles = '\n'.join(f'  - {move.title} ({verbs[move.to_state]})' for move in applied)
    emit(sink, 'moved', provider, total=len(applied), message='; '.join(pieces), detail=titles)


def _replace_older_copies(provider: Provider, out_dir: Path, args, sink: EventSink) -> None:
    """After an update of every chat, replace older copies of a chat by the current one.

    Only when the setting is on, and only for a run that has seen the whole
    provider: a title, `--skip`, `--limit`, `--only-id` or URL run has not."""
    if (args.url or args.title or args.skip or args.limit or getattr(args, 'only_ids', None)
            or should_stop() or not config.retire_older_copies()):
        return
    from . import tidy
    try:
        replaced, kept = tidy.retire_older_copies(out_dir, provider.name)
    except OSError as exc:
        emit(sink, 'warning', provider.name,
             message=f'could not replace older copies of chats: {exc}')
        return
    if replaced:
        emit(sink, 'replaced', provider.name, total=len(replaced),
             message=(f'replaced {_plural(len(replaced), "older copy", "older copies")} of '
                      'chats that were saved in more than one folder'),
             detail='\n'.join(f'  - {copy.title}' for copy in replaced))
    if kept:
        emit(sink, 'warning', provider.name,
             message=(f'kept {_plural(len(kept), "older copy", "older copies")} of chats saved '
                      'in more than one folder: each holds something the current copy lacks '
                      '(review them with Tidy duplicates)'))


def _archived_refs(provider: Provider, session) -> list[ChatRef] | None:
    archived_api = getattr(provider, 'archived_listing', None)
    if not callable(archived_api):
        return None
    refs = archived_api(session)
    return [r for r in refs if isinstance(r, ChatRef)] if isinstance(refs, list) else None


def _run_lifecycle(provider: Provider, session, refs: list[ChatRef], out_dir: Path,
                   args, sink: EventSink, *, dry_run: bool) -> tuple[list[ChatRef], bool]:
    """Plan and preview/apply provider lifecycle moves during unfiltered runs."""
    if (args.url or args.title or args.skip or args.limit or args.rebuild
            or not out_dir.exists()):
        return refs, False
    from . import lifecycle
    sync = SyncState(out_dir, provider.name, migrate=not dry_run)
    if not sync.records():
        return refs, False
    archived = _archived_refs(provider, session)
    if should_stop():
        return refs, False
    lplan = lifecycle.plan_lifecycle(
        out_dir, provider.name, refs, sync,
        archived_refs=archived,
        confirm=_confirm_callback(provider, session),
        stop=should_stop,
        provider_obj=provider, session=session,
    )
    if should_stop():
        return refs, False
    _emit_and_apply_lifecycle(lplan, out_dir, sync, args, provider, sink, dry_run=dry_run)
    filtered = [r for r in refs if not (r.state == 'archived' and sync.known(r.id) is not None)]
    return filtered, bool(lplan.moves or lplan.brake_triggered)


def _dry_run_session(provider: Provider, session, refs: list[ChatRef], out_dir: Path,
                     args, sink: EventSink, *, skip_unchanged: bool) -> int:
    if should_stop():
        emit(sink, 'stopped', provider.name, current=0, total=len(refs),
             message=f'Stopped before reporting plan (0 of {len(refs)} {provider.noun})')
        return 0
    refs, had_lifecycle = _run_lifecycle(provider, session, refs, out_dir, args, sink, dry_run=True)
    if should_stop():
        emit(sink, 'stopped', provider.name, current=0, total=len(refs),
             message=f'Stopped before reporting plan (0 of {len(refs)} {provider.noun})')
        return 0
    if not refs and not had_lifecycle:
        emit(sink, 'nothing_matched', provider.name, message='No chats matched.')
        return 1
    sync = SyncState(out_dir, provider.name, migrate=False)
    chats = [{'id': r.id, 'title': r.title, 'project': r.listed_project(), 'state': r.state}
             for r in refs]
    return report_dry_run(chats, {r.id: r.updated_at for r in refs}, sync,
                          skip_unchanged, sink=sink, provider=provider.name)


def _session(provider: Provider, page, args, sink: EventSink) -> int:
    """Log in, list or take the URL, filter, then list, preview or download."""
    name = provider.name
    out_dir = Path(args.output_dir)
    started = time.time()
    session = provider.open(page)
    if args.url:
        refs = [replace(provider.from_url(session, args.url), listed=False)]
    else:
        emit(sink, 'listing', name, message=provider.listing_text())
        refs = provider.listing(session)
        emit(sink, 'listed', name, total=len(refs), message=provider.found_text(refs))
    refs = select(refs, args)
    if args.list:
        if not refs:
            emit(sink, 'nothing_matched', name, message='No chats matched.')
            return 1
        for n, ref in enumerate(refs, 1):
            emit(sink, 'chat_listed', name, ref.title or ref.id, n, len(refs))
        emit(sink, 'notice', name, message=f'-- {len(refs)} chat(s)')
        return 0
    skip_unchanged = not args.rebuild and not args.title
    if args.dry_run:
        return _dry_run_session(provider, session, refs, out_dir, args, sink,
                                skip_unchanged=skip_unchanged)
    if should_stop():
        emit(sink, 'stopped', name, current=0, total=len(refs),
             message=f'Stopped before downloading (0 of {len(refs)} {provider.noun})')
        return 0
    refs, had_lifecycle = _run_lifecycle(provider, session, refs, out_dir, args, sink, dry_run=False)
    if should_stop():
        emit(sink, 'stopped', name, current=0, total=len(refs),
             message=f'Stopped before downloading (0 of {len(refs)} {provider.noun})')
        return 0
    if not refs and not had_lifecycle:
        emit(sink, 'nothing_matched', name, message='No chats matched.')
        return 1
    out_dir.mkdir(parents=True, exist_ok=True)
    emit(sink, 'downloading', name, total=len(refs), message=f'{len(refs)} {provider.noun}')
    only = getattr(args, 'only_ids', None)
    results = download(provider, session, refs, out_dir, skip_unchanged=skip_unchanged,
                       save_raw=not args.no_raw, sink=sink,
                       only_ids=set(only) if only else None)
    _replace_older_copies(provider, out_dir, args, sink)
    return finish_run(out_dir, name, results, started, sink)


def check(provider: Provider, page, sink: EventSink | None = None) -> int:
    """Live check for diagnostics: log in and list the chats, saving nothing."""
    sink = resolve(sink)
    with routed(sink):
        session = provider.open(page)
        refs = provider.listing(session)
    emit(sink, 'checked', provider.name,
         message=f'logged in · {len(refs)} {provider.noun} listed')
    return 0


def capture(provider: Provider, page, url: str, destination: Path,
            sink: EventSink | None = None) -> int:
    """Record one chat's responses with its text replaced (see capture.py)."""
    from . import capture as capture_mod, retry
    sink = resolve(sink)
    error: BaseException | None = None
    got: Fetched | None = None
    with routed(sink), retry.recording() as responses:
        try:
            session = provider.open(page)
            ref = provider.from_url(session, url)
            got = provider.fetch(session, ref)
        except Exception as exc:
            error = exc
    saved = capture_mod.save(destination, provider.name, responses, error)
    if error is not None:
        emit(sink, 'warning', provider.name,
             message=f'fetch failed ({error}); the capture was still saved')
    elif got is not None and got.extra.get('path') in ('intercept', 'dom'):
        emit(sink, 'warning', provider.name,
             message=f'used {got.extra["path"]} fallback; only the direct RPC attempt was recorded')
    count = len(responses)
    noun = 'response' if count == 1 else 'responses'
    state = ', fetch failed' if error is not None else ''
    emit(sink, 'captured', provider.name,
         message=f'{count} {noun} recorded{state}', detail=str(saved))
    return 0


def _download_one(provider: Provider, session, ref: ChatRef, index: int, total: int,
                  out_dir: Path, sync: SyncState, *, save_raw: bool,
                  sink: EventSink) -> dict:
    """Fetch and save one chat; a failing chat keeps its sync record untouched."""
    name = provider.name
    label = ref.title or ref.id
    t0 = time.time()
    try:
        got = provider.fetch(session, ref)
        stats = save_chat(got.chat, out_dir, sync, ref.id, ref.updated_at,
                          got.raw if save_raw else None, sink=sink, provider=name,
                          state=ref.state)
        detail = got.detail or f'assets: {stats["images"] + stats["docs"]}'
        emit(sink, 'chat_completed', name, got.chat.title or label, index, total,
             stats_text(stats), f'{detail} · {time.time() - t0:.1f}s')
        return {'id': ref.id, 'title': got.chat.title, 'ok': True, **got.extra,
                'duration_s': round(time.time() - t0, 1),
                **{k: stats[k] for k in ('turns', 'chars', 'images', 'docs')}}
    except Exception as exc:
        emit(sink, 'error', name, label, index, total, str(exc), traceback.format_exc())
        return {'id': ref.id, 'title': label, 'ok': False, 'error': str(exc)}


def download(provider: Provider, session, refs: list[ChatRef], out_dir: Path, *,
             skip_unchanged: bool = False, save_raw: bool = True, log: bool = False,
             sink: EventSink | None = None,
             stop: threading.Event | None = None,
             only_ids: set[str] | None = None) -> list[dict]:
    """Fetch and save `refs`. Returns per-chat results for the manifest.

    Reports a chat_started and then a chat_skipped, chat_completed or error
    event per chat (`log`: a verbose terminal when no sink or route is set).
    One failing chat never stops the others, and it keeps its previous sync
    record, so the next update retries it. When `stop` (or `stoppable()`) is
    set, stops cleanly before starting the next chat. `only_ids` restricts
    downloads to a chosen subset of chat IDs."""
    sink = resolve(sink, verbose=log)
    name = provider.name
    out_dir = Path(out_dir)
    sync = SyncState(out_dir, name)
    results: list[dict] = []
    if only_ids is not None:
        refs = [r for r in refs if r.id in only_ids]
    total = len(refs)
    for i, ref in enumerate(refs, 1):
        if should_stop(stop):
            emit(sink, 'stopped', name, current=i - 1, total=total,
                 message=f'Stopped after {i - 1} of {total} {provider.noun}')
            break
        label = ref.title or ref.id
        emit(sink, 'chat_started', name, label, i, total)
        if skip_unchanged and sync.is_unchanged(
                ref.id, ref.updated_at, title=ref.title or None,
                project=ref.listed_project(), state=ref.state):
            emit(sink, 'chat_skipped', name, label, i, total, 'skip (unchanged)')
            continue
        results.append(_download_one(provider, session, ref, i, total, out_dir,
                                     sync, save_raw=save_raw, sink=sink))
        if not should_stop(stop):
            time.sleep(provider.pace)
    sync.save()
    return results


def main(provider: Provider, argv=None) -> int:
    """`python -m reclaim.providers.<name>`: the same path as `reclaim <name>`.
    Without `argv`, the process arguments (an explicit [] means none)."""
    from .service import ArchiveController
    args = sys.argv[1:] if argv is None else list(argv)
    summary = ArchiveController(config.default_root()).run_cli([provider.name], args,
                                                               headers=False)
    return summary.codes[0][1] if summary.codes else 2
