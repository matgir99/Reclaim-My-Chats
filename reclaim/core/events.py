"""Structured progress shared by the CLI backend and desktop frontend."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class ProgressEvent:
    kind: str
    provider: str = ''
    title: str = ''
    current: int = 0
    total: int = 0
    message: str = ''


EventSink = Callable[[ProgressEvent], None]


def emit(sink: EventSink | None, kind: str, provider: str = '',
         title: str = '', current: int = 0, total: int = 0,
         message: str = '') -> None:
    if sink is not None:
        sink(ProgressEvent(kind, provider, title, current, total, message))
