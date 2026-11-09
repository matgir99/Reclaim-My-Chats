"""The provider engine: everything the providers do the same way.

A provider knows only its service: how to log in, list its chats, turn a
chat URL into a reference and fetch one chat (see `Provider`). The engine
does the rest identically for all of them: command-line options, the title,
`--skip` and `--limit` filters, `--list` and `--dry-run`, skipping unchanged
chats, saving each chat (folder, raw.json, sync record), progress events and
output, the run manifest and the exit code. A fix made here reaches every
provider.
"""

from __future__ import annotations

import argparse
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import config
from .events import EventSink, emit
from .manifest import SyncState, print_dry_run, save_chat, write_manifest
from .model import Chat
from .progress import progress

RULE = '=' * 50


@dataclass
class ChatRef:
    """One chat as the provider lists it."""
    id: str                # the id its sync record uses
    title: str = ''
    updated_at: Any = None  # the provider's own timestamp, if it has one
    data: Any = None       # the provider's listing entry, handed back to fetch()


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

    def announce_listing(self) -> None:
        print(f'Listing {self.noun}...')

    def announce_found(self, refs: list[ChatRef], /) -> None:
        print(f'Found {len(refs)} {self.noun}')


def build_parser(provider: Provider) -> argparse.ArgumentParser:
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
    0 all saved, 1 nothing matched, 2 some chats failed."""
    out_dir = Path(args.output_dir)
    started = time.time()
    session = provider.open(page)
    if args.url:
        refs = [provider.from_url(session, args.url)]
    else:
        provider.announce_listing()
        refs = provider.listing(session)
        provider.announce_found(refs)
    refs = select(refs, args)
    if not refs:
        print('No chats matched.')
        return 1
    if args.list:
        for n, ref in enumerate(refs, 1):
            print(f'{n}. {ref.title or ref.id}')
        print(f'-- {len(refs)} chat(s)')
        return 0
    skip_unchanged = not args.rebuild and not args.title
    if args.dry_run:
        sync = SyncState(out_dir, provider.name, migrate=False)
        return print_dry_run([{'id': r.id, 'title': r.title} for r in refs],
                             {r.id: r.updated_at for r in refs}, sync, skip_unchanged,
                             log=args.log, sink=sink, provider=provider.name)

    out_dir.mkdir(parents=True, exist_ok=True)
    print(f'\n{RULE}\n  {len(refs)} {provider.noun}\n{RULE}\n')
    results = download(provider, session, refs, out_dir, skip_unchanged=skip_unchanged,
                       save_raw=not args.no_raw, log=args.log, sink=sink)
    manifest = write_manifest(out_dir, provider.name, results, started)
    ok = sum(1 for r in results if r.get('ok'))
    print(f'\n{RULE}')
    print(f'  Done: {ok} ok, {len(results) - ok} failed')
    print(f'  Manifest: {manifest}')
    print(RULE)
    return 0 if ok == len(results) else 2


def check(provider: Provider, page, sink: EventSink | None = None) -> int:
    """Live check for diagnostics: log in and list the chats, saving nothing."""
    session = provider.open(page)
    refs = provider.listing(session)
    message = f'logged in · {len(refs)} {provider.noun} listed'
    print(f'{provider.name}: {message}')
    emit(sink, 'checked', provider.name, message=message)
    return 0


def download(provider: Provider, session, refs: list[ChatRef], out_dir: Path, *,
             skip_unchanged: bool = False, save_raw: bool = True, log: bool = False,
             sink: EventSink | None = None) -> list[dict]:
    """Fetch and save `refs`. Returns per-chat results for the manifest.

    Per-chat lines print only with log=True (the default output is
    summary-level); failures always print. One failing chat never stops the
    others, and it keeps its previous sync record, so the next update
    retries it."""
    out_dir = Path(out_dir)
    sync = SyncState(out_dir, provider.name)
    results: list[dict] = []
    started = time.time()
    total = len(refs)
    for i, ref in enumerate(refs, 1):
        label = (ref.title or ref.id)[:55]
        emit(sink, 'chat_started', provider.name, label, i, total)
        if log:
            print(progress(i, total, started))
        if skip_unchanged and sync.is_unchanged(ref.id, ref.updated_at):
            if log:
                print(f'[{i}/{total}] {label} -> skip (unchanged)')
            emit(sink, 'chat_skipped', provider.name, label, i, total)
            continue
        t0 = time.time()
        try:
            got = provider.fetch(session, ref)
            stats = save_chat(got.chat, out_dir, sync, ref.id, ref.updated_at,
                              got.raw if save_raw else None, sink=sink, provider=provider.name)
            results.append({'id': ref.id, 'title': got.chat.title, 'ok': True, **got.extra,
                            'duration_s': round(time.time() - t0, 1),
                            **{k: stats[k] for k in ('turns', 'chars', 'images', 'docs')}})
            emit(sink, 'chat_completed', provider.name, got.chat.title, i, total)
            if log:
                media = f", {stats['images']} img" if stats['images'] else ''
                media += f", {stats['docs']} doc" if stats['docs'] else ''
                print(f'[{i}/{total}] {label} -> {stats["turns"]}t, {stats["chars"]:,} chars{media}')
                detail = got.detail or f'assets: {stats["images"] + stats["docs"]}'
                print(f'    {detail} · {time.time() - t0:.1f}s')
        except Exception as exc:
            print(f'[{i}/{total}] {label} -> FAILED: {exc}')
            traceback.print_exc()
            results.append({'id': ref.id, 'title': label, 'ok': False, 'error': str(exc)})
            emit(sink, 'error', provider.name, label, i, total, str(exc))
        time.sleep(provider.pace)
    sync.save()
    return results


def main(provider: Provider, argv=None) -> int:
    """`python -m reclaim.providers.<name>`: the same path as `reclaim <name>`."""
    from .service import ArchiveController
    summary = ArchiveController(config.default_root()).run_cli([provider.name], list(argv or []),
                                                               headers=False)
    return summary.codes[0][1] if summary.codes else 2
