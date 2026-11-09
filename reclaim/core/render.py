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
        kind, message = event.kind, event.message
        title = event.title[:TITLE_WIDTH]
        if kind == 'provider_started':
            return f'\n========== {event.provider} ==========' if self.headers else None
        if kind in ('run_started', 'run_completed', 'provider_completed'):
            return None
        if kind == 'login_needed':
            return f'\n{RULE}\n  {message}\n{RULE}\n'
        if kind == 'downloading':
            self._started = time.time()
            return f'\n{RULE}\n  {message}\n{RULE}\n'
        if kind == 'downloaded':
            return '\n'.join((f'\n{RULE}', f'  {message}',
                              *([f'  {event.detail}'] if event.detail else []), RULE))
        if kind == 'chat_listed':
            return f'{event.current}. {event.title}'
        if kind == 'chat_started':
            return (progress(event.current, event.total, self._started)
                    if self.verbose and event.total else None)
        if kind in ('chat_skipped', 'chat_planned', 'chat_completed'):
            if not self.verbose:
                return None
            line = f'{_position(event)}{title} -> {message}'
            return f'{line}\n    {event.detail}' if event.detail else line
        if kind == 'error':
            where = (f'{_position(event)}{title} -> ' if event.title
                     else f'{event.provider}: ' if event.provider else '')
            line = f'{where}FAILED: {message}'
            if self.verbose and event.detail:
                line += '\n' + event.detail.rstrip()
            return line
        if kind == 'warning':
            return f'  warning: {title}: {message}' if title else f'warning: {message}'
        if kind == 'plan':
            return message if self.verbose or not event.detail else f'{message}\n{event.detail}'
        if kind == 'checked':
            return f'{event.provider}: {message}'
        return message or None


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
