"""Progress events: the only way a run reports what it is doing.

Code that has something to report emits an event; it never prints. Each
front end renders the events once (see render.py): the terminal for
commands, the window for the app, and the log file for both. The kinds:

  run_started / run_completed         a run (message: the mode / 'exit N')
  provider_started / provider_completed
                                      one provider (current/total: its place in
                                      the run; message 'exit N' at the end)
  notice                              worth a line: the browser used, a database
                                      recognised
  login_needed / logged_in            the browser window waits for the user to
                                      log in (message: what to do)
  listing / listed                    listing the chats (listed: total, message)
  chat_listed                         --list: one title (current/total)
  nothing_matched
  chat_planned / plan                 --dry-run: one chat's action, then the
                                      counts (message) and titles (detail)
  downloading / downloaded            saving starts (total) and ends (message:
                                      the counts, detail: the run record)
  chat_started / chat_skipped / chat_completed
                                      one chat (current/total, title; message:
                                      the outcome; detail: timing)
  warning                             something was not saved; the run goes on
  error                               a chat or a provider failed (detail: the
                                      traceback, for the log)
  checked                             the live check's result for a provider

Where events go is set per run with `routed()`. Without a route (a library
call, a test), they are printed as a command would print them.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass


@dataclass(frozen=True)
class ProgressEvent:
    kind: str
    provider: str = ''
    title: str = ''
    current: int = 0
    total: int = 0
    message: str = ''
    detail: str = ''


EventSink = Callable[[ProgressEvent], None]

# Per thread, not per context: Playwright's sync API runs callbacks in other
# greenlets of the same thread, which must report to the same place.
_route = threading.local()


def routed_sink() -> EventSink | None:
    """Where this thread's run sends its events, if a run has set it."""
    return getattr(_route, 'sink', None)


@contextmanager
def routed(sink: EventSink) -> Iterator[EventSink]:
    """Send every event emitted in this thread during the block to `sink`."""
    previous = routed_sink()
    _route.sink = sink
    try:
        yield sink
    finally:
        _route.sink = previous


def resolve(sink: EventSink | None = None, *, verbose: bool = False) -> EventSink:
    """The sink to report to: the one given, else the run's route, else the
    terminal (with per-chat lines when `verbose`)."""
    if sink is not None:
        return sink
    route = routed_sink()
    if route is not None:
        return route
    from .render import TerminalRenderer
    return TerminalRenderer(verbose=verbose)


def emit(sink: EventSink | None, kind: str, provider: str = '',
         title: str = '', current: int = 0, total: int = 0,
         message: str = '', detail: str = '') -> None:
    resolve(sink)(ProgressEvent(kind, provider, title, current, total, message, detail))
