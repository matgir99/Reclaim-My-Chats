"""The native-provider operation boundary used by CLI and GUI."""

from __future__ import annotations

import threading
import traceback
from dataclasses import dataclass
from pathlib import Path

from . import browser, config
from .events import EventSink, emit

PROVIDER_DIRS = {
    'googleaistudio': 'Google AI Studio',
    'deepseek': 'Deepseek Chat',
    'kimi': 'Kimi Chat',
    'chatgpt': 'ChatGPT',
    'claude': 'Claude',
    'googlegemini': 'Google Gemini',
}
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
            raise BusyOperationError('another archive operation is running')
        try:
            flags = {'update': [], 'rebuild': ['--rebuild'],
                     'dry-run': ['--dry-run']}[mode]
            archive = config.archive_root(self.root)
            parsed = {}
            for name in chosen:
                out_dir = archive / PROVIDER_DIRS[name]
                parsed[name] = provider_module(name).parse_args(
                    [*flags, '-o', str(out_dir), *(['--log'] if log else [])])
            return self._execute(chosen, parsed, mode, sink)
        finally:
            self._lock.release()

    def run_cli(self, providers: list[str], argv: list[str]) -> RunSummary:
        """Preserve the existing `reclaim all` argument and output contract."""
        if not self._lock.acquire(blocking=False):
            raise BusyOperationError('another archive operation is running')
        try:
            parsed = {name: provider_module(name).parse_args(list(argv))
                      for name in providers}
            return self._execute(providers, parsed, 'cli', None)
        finally:
            self._lock.release()

    def _execute(self, chosen: list[str], parsed: dict, mode: str,
                 sink: EventSink | None) -> RunSummary:
        from playwright.sync_api import sync_playwright

        emit(sink, 'run_started', total=len(chosen), message=mode)
        codes: list[tuple[str, int]] = []
        with sync_playwright() as playwright:
            ctx, page = browser.launch(playwright, profile_dir=config.profile_root(self.root))
            try:
                for index, name in enumerate(chosen, 1):
                    if mode == 'cli':
                        print(f'\n========== {name} ==========')
                    emit(sink, 'provider_started', name, current=index,
                         total=len(chosen))
                    try:
                        code = provider_module(name).run_session(page, parsed[name], sink=sink)
                    except Exception as exc:
                        traceback.print_exc()
                        code = 2
                        emit(sink, 'error', name, message=str(exc))
                    codes.append((name, code))
                    emit(sink, 'provider_completed', name, current=index,
                         total=len(chosen), message=f'exit {code}')
            finally:
                ctx.close()
        summary = RunSummary(tuple(codes))
        emit(sink, 'run_completed', current=len(chosen), total=len(chosen),
             message=f'exit {summary.exit_code}')
        return summary
