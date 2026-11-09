"""The native-provider operation boundary used by CLI and GUI.

Every run, from the window or any command, passes the same checks before a
browser opens: the settings are valid, the database folder is usable (see
database.prepare), the login data folder and the database are locked
against other runs, and everything printed goes to the log file.
"""

from __future__ import annotations

import threading
import traceback
import time
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path

from . import browser, config, database, locks, logs
from .database import PROVIDER_DIRS
from .events import EventSink, emit
from .manifest import write_attempt

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
def offline_session(db: Path, label: str, root: Path | None = None):
    """Checks for commands that write without a browser (imports, parsers):
    a usable database folder `db`, its lock, and the log file."""
    root = Path(root) if root is not None else config.default_root()
    db = Path(db)
    with logs.capture(root, label):
        database.prepare(db, create=_may_create(root, db))
        with locks.hold(db, 'update'):
            yield db


class ArchiveController:
    def __init__(self, root: Path | None = None):
        self.root = Path(root) if root is not None else config.default_root()
        self._lock = threading.Lock()

    @property
    def busy(self) -> bool:
        return self._lock.locked()

    def run(self, providers: list[str], mode: str = 'update',
            sink: EventSink | None = None, *, log: bool = False) -> RunSummary:
        """Run a selected set in one Playwright session, off the GUI loop."""
        if mode not in ('update', 'rebuild', 'dry-run'):
            raise ValueError(f'unknown mode: {mode}')
        chosen = list(dict.fromkeys(providers))
        if not chosen or any(p not in PROVIDER_DIRS for p in chosen):
            raise ValueError('choose at least one known provider')
        if not self._lock.acquire(blocking=False):
            raise BusyOperationError('another operation is running')
        try:
            flags = {'update': [], 'rebuild': ['--rebuild'],
                     'dry-run': ['--dry-run']}[mode]
            archive = config.archive_root(self.root)
            parsed = {}
            for name in chosen:
                out_dir = archive / PROVIDER_DIRS[name]
                parsed[name] = provider_module(name).parse_args(
                    [*flags, '-o', str(out_dir), *(['--log'] if log else [])])
            return self._execute(chosen, parsed, mode, sink, {archive},
                                 readonly=mode == 'dry-run')
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
            return self._execute(providers, parsed, 'cli' if headers else 'cli-single', None,
                                 roots, readonly=readonly)
        finally:
            self._lock.release()

    def _execute(self, chosen: list[str], parsed: dict, mode: str,
                 sink: EventSink | None, roots: set[Path], *, readonly: bool) -> RunSummary:
        label = f'{mode} {", ".join(chosen)}'
        with ExitStack() as stack:
            stack.enter_context(logs.capture(self.root, label))
            for db in sorted(roots):
                database.prepare(db, create=_may_create(self.root, db), readonly=readonly)
            profile = config.profile_root(self.root)
            stack.enter_context(locks.hold(profile, 'browser'))
            if not readonly:
                for db in sorted(roots):
                    stack.enter_context(locks.hold(db, 'update'))
            return self._run_providers(chosen, parsed, mode, sink, profile)
        raise AssertionError('unreachable')  # ExitStack never suppresses here

    def _run_providers(self, chosen: list[str], parsed: dict, mode: str,
                       sink: EventSink | None, profile: Path) -> RunSummary:
        from playwright.sync_api import sync_playwright

        emit(sink, 'run_started', total=len(chosen), message=mode)
        codes: list[tuple[str, int]] = []
        with sync_playwright() as playwright:
            ctx, page = browser.launch(playwright, profile_dir=profile)
            try:
                for index, name in enumerate(chosen, 1):
                    codes.append((name, self._run_one(name, parsed[name], mode, sink,
                                                      page, index, len(chosen))))
            finally:
                ctx.close()
        summary = RunSummary(tuple(codes))
        emit(sink, 'run_completed', current=len(chosen), total=len(chosen),
             message=f'exit {summary.exit_code}')
        return summary

    def _run_one(self, name: str, args, mode: str, sink: EventSink | None,
                 page, index: int, total: int) -> int:
        started = time.time()
        if mode == 'cli':
            print(f'\n========== {name} ==========')
        emit(sink, 'provider_started', name, current=index, total=total)
        error = None
        try:
            code = provider_module(name).run_session(page, args, sink=sink)
        except Exception as exc:
            traceback.print_exc()
            code = 2
            error = str(exc)
            emit(sink, 'error', name, message=str(exc))
        if mode != 'dry-run' and not _readonly(args):
            out = Path(getattr(args, 'output_dir',
                               config.archive_root(self.root) / PROVIDER_DIRS[name]))
            try:
                write_attempt(out, started, code, error)
            except OSError as save_error:
                code = 2
                emit(sink, 'error', name,
                     message=f'Could not save run status: {save_error}')
        emit(sink, 'provider_completed', name, current=index,
             total=total, message=f'exit {code}')
        return code
