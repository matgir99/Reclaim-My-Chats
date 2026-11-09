"""Run log: a rotating log file in the data folder.

Every run, from the window or the command line, copies what it prints to
`<data folder>/logs/reclaim.log`: provider progress, warnings and error
tracebacks. The file rotates at 1 MB and keeps five older files. It stays
on this computer. It contains chat titles and error details, never chat
text or login data. The packaged desktop app has no terminal, so this log
is the only record of what a run did.
"""

from __future__ import annotations

import io
import logging
import logging.handlers
import sys
import threading
from contextlib import contextmanager
from pathlib import Path

from . import config

LOG_NAME = 'reclaim.log'
_handler: logging.Handler | None = None
_handler_path: Path | None = None


def log_dir(root: Path | None = None) -> Path:
    return (Path(root) if root is not None else config.default_root()) / 'logs'


def log_file(root: Path | None = None) -> Path:
    return log_dir(root) / LOG_NAME


def get_logger(root: Path | None = None) -> logging.Logger:
    """The app logger, writing to this data folder's log file."""
    global _handler, _handler_path
    path = log_file(root)
    log = logging.getLogger('reclaim')
    log.setLevel(logging.INFO)
    log.propagate = False
    if _handler_path != path:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            path, maxBytes=1_000_000, backupCount=5, encoding='utf-8', delay=True)
        handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)-7s %(message)s'))
        if _handler is not None:
            log.removeHandler(_handler)
            _handler.close()
        log.addHandler(handler)
        _handler, _handler_path = handler, path
    return log


class _Tee(io.TextIOBase):
    """A text stream that writes through and logs each complete line."""

    def __init__(self, stream, log: logging.Logger, level: int):
        super().__init__()
        self._stream = stream
        self._log = log
        self._level = level
        self._pending = ''
        self._lock = threading.Lock()

    def writable(self) -> bool:
        return True

    def write(self, text) -> int:
        text = str(text)
        if self._stream is not None:
            try:
                self._stream.write(text)
            except (OSError, ValueError):
                pass
        with self._lock:
            *lines, self._pending = (self._pending + text).split('\n')
        for line in lines:
            if line.strip():
                self._log.log(self._level, line.rstrip())
        return len(text)

    def flush(self) -> None:
        if self._stream is not None:
            try:
                self._stream.flush()
            except (OSError, ValueError):
                pass

    def isatty(self) -> bool:
        return bool(self._stream is not None and self._stream.isatty())

    @property
    def encoding(self):  # type: ignore[override]
        return getattr(self._stream, 'encoding', None) or 'utf-8'

    def fileno(self) -> int:
        if self._stream is None:
            raise io.UnsupportedOperation('fileno')
        return self._stream.fileno()

    def finish(self) -> None:
        with self._lock:
            rest, self._pending = self._pending, ''
        if rest.strip():
            self._log.log(self._level, rest.rstrip())


@contextmanager
def capture(root: Path | None, label: str):
    """Copy everything printed during the block to the log file.

    If the log cannot be opened (for example a read-only data folder), the
    run continues without it."""
    try:
        log = get_logger(root)
    except OSError:
        yield None
        return
    out, err = sys.stdout, sys.stderr
    tee_out, tee_err = _Tee(out, log, logging.INFO), _Tee(err, log, logging.WARNING)
    sys.stdout, sys.stderr = tee_out, tee_err
    log.info('=== %s: started', label)
    try:
        yield log
    except BaseException:
        log.exception('=== %s: stopped by an error', label)
        raise
    finally:
        tee_out.finish()
        tee_err.finish()
        sys.stdout, sys.stderr = out, err
        log.info('=== %s: finished', label)
