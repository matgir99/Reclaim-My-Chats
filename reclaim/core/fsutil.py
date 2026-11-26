"""Crash-safe file writes.

Every file the database depends on is written to a temporary file in the
same folder and then renamed over the target. A crash, a full disk or a
closed window leaves either the old file or the new one, never a truncated
mix. Temporary names start with a dot and end in `.tmp`, so they never
match `chat.json` or `*.md` scans.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any


def _is_windows() -> bool:
    return os.name == 'nt'


def windows_retry_op(op: Callable[[], Any],
                     delays: tuple[float, ...] = (0.05, 0.1, 0.25)) -> None:
    """Retry transient Windows sharing/scanner locks (`WinError 5` / `32`)."""
    if not _is_windows():
        op()
        return
    for delay in delays:
        try:
            op()
            return
        except PermissionError as exc:
            if getattr(exc, 'winerror', None) not in (5, 32):
                raise
            time.sleep(delay)
    op()


def atomic_write_bytes(path: Path, data: bytes, *, mode: int | None = None) -> None:
    """Replace `path` with `data` in one step."""
    path = Path(path)
    fd, tmp = tempfile.mkstemp(prefix=f'.{path.name[:40]}.', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if mode is not None and not _is_windows():
            os.chmod(tmp, mode)
        windows_retry_op(lambda: os.replace(tmp, path))
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def atomic_write_text(path: Path, text: str, *, mode: int | None = None) -> None:
    """Replace `path` with UTF-8 `text` in one step."""
    atomic_write_bytes(path, text.encode('utf-8'), mode=mode)
