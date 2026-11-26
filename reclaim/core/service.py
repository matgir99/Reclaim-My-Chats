"""The native-provider operation boundary used by CLI and GUI.

Every run, from the window or any command, passes the same checks before a
browser opens: the settings are valid, the database folder is usable (see
database.prepare), the login data folder and the database are locked
against other runs, and the run's events go to the log file as well as to
the terminal or the window (see events.py and render.py). Anything else
printed, such as a library's warning, is logged too.
"""

from __future__ import annotations

import contextlib
import sys
import threading
import traceback
import time
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

from . import browser, config, database, locks, logs
from .database import PROVIDER_DIRS
from .events import EventSink, emit, routed
from .render import LogRenderer, TerminalRenderer, fanout
from .manifest import record_check, write_attempt

ALL_PROVIDERS = list(PROVIDER_DIRS)


def provider_module(name: str):
    """Explicit imports keep every provider visible to PyInstaller."""
    from ..providers import chatgpt, claude, deepseek, googleaistudio, googlegemini, kimi
    return {
        'googleaistudio': googleaistudio,
        'deepseek': deepseek,
        'kimi': kimi,
        'chatgpt': chatgpt,
        'claude': claude,
        'googlegemini': googlegemini,
    }[name]


@dataclass(frozen=True)
class RunSummary:
    codes: tuple[tuple[str, int], ...]

    @property
    def exit_code(self) -> int:
        return 0 if self.codes and all(code == 0 for _, code in self.codes) else 2


class BusyOperationError(RuntimeError):
    """A desktop operation is already using the browser profile."""


def _readonly(args) -> bool:
    """True for runs that write nothing: previews and title lists."""
    return bool(getattr(args, 'dry_run', False) or getattr(args, 'list', False))


def _may_create(root: Path, db: Path) -> bool:
    """Only the default database location is created on first use."""
    return config.uses_default_database(root) and Path(db) == config.archive_root(root)


@contextmanager
def _reporting(root: Path, label: str, sink: EventSink | None):
    """The run's log file, with its events sent to `sink` and to the log."""
    with logs.capture(root, label) as log, \
            routed(fanout(sink, LogRenderer(log) if log is not None else None)) as route:
        yield route


@contextmanager
def offline_session(db: Path, label: str, root: Path | None = None):
    """Checks for commands that write without a browser (imports, parsers):
    a usable database folder `db`, its lock, and the log file. Events go to
    the terminal, a line per chat, and to the log."""
    root = Path(root) if root is not None else config.default_root()
    db = Path(db)
    with _reporting(root, label, TerminalRenderer(sys.stdout, verbose=True)):
        database.prepare(db, create=_may_create(root, db), adopt_legacy=False)
        with locks.hold(db, 'update'):
            database.prepare(db)  # marks an older database only under its lock
            yield db


class ArchiveController:
    def __init__(self, root: Path | None = None):
        self.root = Path(root) if root is not None else config.default_root()
        self._lock = threading.Lock()
        self._stop = threading.Event()

    @property
    def busy(self) -> bool:
        return self._lock.locked()

    def stop(self) -> None:
        """Request the active run to stop after the current chat."""
        self._stop.set()

    def bring_browser_forward(self) -> bool:
        """Bring the active browser window to the front for login."""
        return browser.bring_browser_forward()

    def run(self, providers: list[str], mode: str = 'update',
            sink: EventSink | None = None, *, log: bool = False,
            only_ids: list[str] | None = None) -> RunSummary:
        """Run a selected set in one Playwright session, off the GUI loop."""
        if mode not in ('update', 'rebuild', 'dry-run', 'check'):
            raise ValueError(f'unknown mode: {mode}')
        chosen = list(dict.fromkeys(providers))
        if not chosen or any(p not in PROVIDER_DIRS for p in chosen):
            raise ValueError('choose at least one known provider')
        if not self._lock.acquire(blocking=False):
            raise BusyOperationError('another operation is running')
        try:
            flags = {'update': [], 'rebuild': ['--rebuild'],
                     'dry-run': ['--dry-run'], 'check': ['--dry-run']}[mode]
            only_flags = [arg for cid in (only_ids or ()) for arg in ('--only-id', cid)]
            archive = config.archive_root(self.root)
            parsed = {}
            for name in chosen:
                out_dir = archive / PROVIDER_DIRS[name]
                parsed[name] = provider_module(name).parse_args(
                    [*flags, *only_flags, '-o', str(out_dir), *(['--log'] if log else [])])
            return self._execute(chosen, parsed, mode, sink, {archive},
                                 readonly=mode in ('dry-run', 'check'))
        finally:
            self._lock.release()

    def capture(self, provider: str, url: str, sink: EventSink | None = None,
                destination: Path | None = None) -> RunSummary:
        """Fetch one chat by URL and save its sanitized responses (see capture.py)."""
        if provider not in PROVIDER_DIRS:
            raise ValueError(f'unknown provider: {provider}')
        url = url.strip()
        if not url:
            raise ValueError('enter a chat URL')
        if not self._lock.acquire(blocking=False):
            raise BusyOperationError('another operation is running')
        try:
            from . import capture as capture_mod
            target = (Path(destination) if destination is not None
                      else capture_mod.default_path(self.root, provider))
            spec = SimpleNamespace(url=url, destination=target, dry_run=True, list=False)
            return self._execute([provider], {provider: spec}, 'capture', sink,
                                 set(), readonly=True)
        finally:
            self._lock.release()

    def run_cli(self, providers: list[str], argv: list[str], *,
                headers: bool = True) -> RunSummary:
        """Command-line runs, one or several providers with the same options."""
        if not self._lock.acquire(blocking=False):
            raise BusyOperationError('another operation is running')
        try:
            parsed = {name: provider_module(name).parse_args(list(argv))
                      for name in providers}
            roots = {Path(args.output_dir).parent for args in parsed.values()}
            readonly = all(_readonly(args) for args in parsed.values())
            # Bound to the terminal now: the log file only gets the events once.
            terminal = TerminalRenderer(sys.stdout, headers=headers,
                                        verbose=any(args.log for args in parsed.values()))
            return self._execute(providers, parsed, 'cli' if headers else 'cli-single',
                                 terminal, roots, readonly=readonly)
        finally:
            self._lock.release()

    def _execute(self, chosen: list[str], parsed: dict, mode: str,
                 sink: EventSink | None, roots: set[Path], *, readonly: bool) -> RunSummary:
        """The checks every run passes, in this order: open the log and route
        the events; check each database folder; lock the login data folder
        (one browser per profile); for runs that write, lock each database
        and only then mark one made by an earlier version. Then run."""
        self._stop.clear()
        label = f'{mode} {", ".join(chosen)}'
        with ExitStack() as stack:
            stack.enter_context(config.use_root(self.root))
            sink = stack.enter_context(_reporting(self.root, label, sink))
            # Validate first (creating the default location on first use): the
            # lock file lives inside the database folder.
            for db in sorted(roots):
                database.prepare(db, create=_may_create(self.root, db), readonly=readonly,
                                 adopt_legacy=False)
            profile = config.profile_root(self.root)
            stack.enter_context(locks.hold(profile, 'browser'))
            if not readonly:
                for db in sorted(roots):
                    stack.enter_context(locks.hold(db, 'update'))
                    database.prepare(db)  # marks an older database only under its lock
            return self._run_providers(chosen, parsed, mode, sink, profile)
        raise AssertionError('unreachable')  # ExitStack never suppresses here

    def _run_providers(self, chosen: list[str], parsed: dict, mode: str,
                       sink: EventSink, profile: Path) -> RunSummary:
        """All chosen providers in one browser window, one after another."""
        from playwright.sync_api import sync_playwright  # imported only when a run starts
        from . import engine

        emit(sink, 'run_started', total=len(chosen), message=mode)
        codes: list[tuple[str, int]] = []
        with sync_playwright() as playwright, engine.stoppable(self._stop):
            ctx, page = browser.launch(playwright, profile_dir=profile, root=self.root)
            try:
                for index, name in enumerate(chosen, 1):
                    if index > 1 and (self._stop.is_set() or browser._page_closed(page)):
                        break
                    codes.append((name, self._run_one(name, parsed[name], mode, sink,
                                                      page, index, len(chosen))))
            finally:
                browser.clear_active_page()
                ctx.close()
        summary = RunSummary(tuple(codes))
        emit(sink, 'run_completed', current=len(chosen), total=len(chosen),
             message=f'exit {summary.exit_code}')
        return summary

    def _run_one(self, name: str, args, mode: str, sink: EventSink,
                 page, index: int, total: int) -> int:
        """One provider. Its failure is reported and becomes its exit code (2),
        so the next provider still runs; a run that may write also records
        this attempt for the status view (`.reclaim_last_attempt.json`)."""
        started = time.time()
        emit(sink, 'provider_started', name, current=index, total=total)
        error = None
        try:
            if mode == 'check':  # diagnostics: log in and list, nothing else
                from . import engine
                code = engine.check(provider_module(name).SPEC, page, sink)
            elif mode == 'capture':
                from . import engine
                code = engine.capture(provider_module(name).SPEC, page,
                                      args.url, args.destination, sink)
            else:
                code = provider_module(name).run_session(page, args, sink=sink)
        except browser.LoginCancelledError:
            code = 0
            emit(sink, 'stopped', name, message='Stopped while waiting for login')
        except browser.LoginRequiredError as exc:
            code = 2
            error = str(exc)  # login_needed event was already emitted by the browser helper
        except Exception as exc:
            code = 2
            error = str(exc)
            emit(sink, 'error', name, message=str(exc), detail=traceback.format_exc())
        if mode != 'dry-run' and not _readonly(args):
            out = Path(getattr(args, 'output_dir',
                               config.archive_root(self.root) / PROVIDER_DIRS[name]))
            try:
                write_attempt(out, started, code, error)
            except OSError as save_error:
                code = 2
                emit(sink, 'error', name,
                     message=f'Could not save run status: {save_error}')
        if code == 0 and mode in ('update', 'rebuild', 'dry-run', 'check', 'cli', 'cli-single'):
            with contextlib.suppress(OSError):
                record_check(self.root, name, started)
        emit(sink, 'provider_completed', name, current=index,
             total=total, message=f'exit {code}')
        return code
