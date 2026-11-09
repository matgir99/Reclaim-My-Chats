"""Turning progress events into text: once for the terminal, once for the
log file, and one shared wording (`describe`) that the log and the window's
Details panel both use. See events.py for the kinds."""

from __future__ import annotations

import logging
import sys
import time
from typing import TextIO

from .events import EventSink, ProgressEvent
from .progress import progress

RULE = '=' * 50
TITLE_WIDTH = 55

# Per-chat progress: the window's progress bar and a verbose terminal show
# it; the log and the Details panel keep only outcomes.
PER_CHAT = frozenset({'chat_started', 'chat_skipped', 'chat_planned', 'chat_listed'})


def describe(event: ProgressEvent) -> str | None:
    """The event in plain words, without the provider's name; None for
    per-chat progress."""
    kind, title, message = event.kind, event.title, event.message
    if kind in PER_CHAT:
        return None
    if kind == 'run_started':
        return f'Run started: {message}'
    if kind == 'run_completed':
        return f'Run completed: {message}'
    if kind == 'provider_started':
        return 'started'
    if kind == 'provider_completed':
        return f'finished ({message})'
    if kind == 'chat_completed':
        return f'{title} — saved ({message})'
    if kind == 'error':
        return f'{title} — FAILED: {message}' if title else f'FAILED: {message}'
    if kind == 'warning':
        return f'warning: {title} — {message}' if title else f'warning: {message}'
    if kind == 'plan':
        return '\n'.join(part for part in (message, event.detail) if part)
    if kind == 'downloaded':
        return f'{message} · {event.detail}' if event.detail else message
    return message or kind.replace('_', ' ')


def fanout(*sinks: EventSink | None) -> EventSink:
    """One sink that hands each event to every given sink, in order."""
    targets = [sink for sink in sinks if sink is not None]

    def send(event: ProgressEvent) -> None:
        for target in targets:
            target(event)
    return send


def _position(event: ProgressEvent) -> str:
    return f'[{event.current}/{event.total}] ' if event.total else ''


class TerminalRenderer:
    """What a command prints. Summary-level by default; `verbose` (--log)
    adds a line per chat and the tracebacks of failures."""

    def __init__(self, stream: TextIO | None = None, *, verbose: bool = False,
                 headers: bool = False):
        self.stream = stream        # None: whatever sys.stdout is when writing
        self.verbose = verbose
        self.headers = headers      # a heading per provider (multi-provider runs)
        self._started = time.time()

    def __call__(self, event: ProgressEvent) -> None:
        text = self.text(event)
        if text is not None:
            print(text, file=self.stream or sys.stdout, flush=True)

    def text(self, event: ProgressEvent) -> str | None:
        """The lines for one event, or None when the terminal skips it."""
        kind = event.kind
        if kind in ('run_started', 'run_completed', 'provider_completed'):
            return None  # the command prints its own summary
        if kind in PER_CHAT or kind == 'chat_completed':
            return self._chat_line(event)
        special = {'provider_started': self._heading, 'login_needed': self._banner,
                   'downloading': self._download_start, 'downloaded': self._download_end,
                   'error': self._failure, 'warning': self._warning, 'plan': self._plan,
                   'checked': lambda e: f'{e.provider}: {e.message}'}.get(kind)
        return special(event) if special else event.message or None

    def _heading(self, event: ProgressEvent) -> str | None:
        return f'\n========== {event.provider} ==========' if self.headers else None

    @staticmethod
    def _banner(event: ProgressEvent) -> str:
        return f'\n{RULE}\n  {event.message}\n{RULE}\n'

    def _download_start(self, event: ProgressEvent) -> str:
        self._started = time.time()  # for the elapsed time and ETA of --log
        return self._banner(event)

    @staticmethod
    def _download_end(event: ProgressEvent) -> str:
        detail = [f'  {event.detail}'] if event.detail else []
        return '\n'.join((f'\n{RULE}', f'  {event.message}', *detail, RULE))

    def _chat_line(self, event: ProgressEvent) -> str | None:
        if event.kind == 'chat_listed':  # --list prints titles in any mode
            return f'{event.current}. {event.title}'
        if not self.verbose:
            return None
        if event.kind == 'chat_started':
            return progress(event.current, event.total, self._started) if event.total else None
        line = f'{_position(event)}{event.title[:TITLE_WIDTH]} -> {event.message}'
        return f'{line}\n    {event.detail}' if event.detail else line

    def _failure(self, event: ProgressEvent) -> str:
        where = (f'{_position(event)}{event.title[:TITLE_WIDTH]} -> ' if event.title
                 else f'{event.provider}: ' if event.provider else '')
        line = f'{where}FAILED: {event.message}'
        if self.verbose and event.detail:  # the traceback
            line += '\n' + event.detail.rstrip()
        return line

    @staticmethod
    def _warning(event: ProgressEvent) -> str:
        title = event.title[:TITLE_WIDTH]
        return f'  warning: {title}: {event.message}' if title else f'warning: {event.message}'

    def _plan(self, event: ProgressEvent) -> str:
        # with --log the per-chat lines already show what would be fetched
        if self.verbose or not event.detail:
            return event.message
        return f'{event.message}\n{event.detail}'


class LogRenderer:
    """The log file's lines: outcomes, warnings and errors with their
    tracebacks, each prefixed with the provider's name."""

    def __init__(self, logger: logging.Logger):
        self.logger = logger

    def __call__(self, event: ProgressEvent) -> None:
        text = describe(event)
        if text is None:
            return
        level = {'error': logging.ERROR, 'warning': logging.WARNING}.get(event.kind,
                                                                        logging.INFO)
        prefix = f'{event.provider}: ' if event.provider else ''
        self.logger.log(level, '%s%s', prefix, text)
        if event.kind == 'error' and event.detail:
            self.logger.log(level, '%s', event.detail.rstrip())
