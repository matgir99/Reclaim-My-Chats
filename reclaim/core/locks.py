"""Lock files: one run at a time per database and per login data folder.

A run holds an operating-system lock on `.reclaim.lock` for as long as it
runs (flock on Linux and macOS, a byte-range lock on Windows) and writes its
computer, process and start time into the file. The operating system
releases the lock when the process ends, even after a crash, so two runs
can never both hold it and an abandoned lock never needs cleaning up on the
same computer.

The file's content also reaches other computers through a synced database
folder. A lock written by another computer cannot be checked from here: the
run stops, and the user removes the lock once that computer is no longer
running Reclaim My Chats. The file itself is never deleted, so a process
waiting on it can never end up holding a lock on a deleted file; an empty
file means unlocked.
"""

from __future__ import annotations

import json
import os
import secrets
import socket
import time
from contextlib import contextmanager
from pathlib import Path

LOCK_NAME = '.reclaim.lock'
# Windows locks are mandatory: lock one byte far beyond the content, so other
# processes can still read who holds the lock.
_WINDOWS_REGION = 1 << 20
_held: dict[str, tuple[int, str]] = {}  # lock file -> (descriptor, token)


class LockedError(RuntimeError):
    """Another run holds the lock."""

    def __init__(self, path: Path, holder: dict, *, removable: bool, here: bool = False):
        self.path = Path(path)
        self.holder = holder
        self.removable = removable  # True when the holder cannot be shown to be running
        what = 'the login data folder' if holder.get('purpose') == 'browser' else 'the database'
        since = holder.get('started') or 'an unknown time'
        if here or holder.get('host') == socket.gethostname():
            process = f'process {holder["pid"]}' if holder.get('pid') else 'another process'
            who = f'another Reclaim My Chats window or command on this computer ({process})'
        else:
            who = f'Reclaim My Chats on {holder.get("host") or "an unknown computer"}'
        advice = ('If that computer is no longer running Reclaim My Chats, remove the lock '
                  'and try again.' if removable else
                  'Wait for it to finish, or close it, then try again.')
        super().__init__(f'{what[0].upper()}{what[1:]} is in use by {who} since {since}. {advice}')


def _try_lock(fd: int) -> bool:
    """Take the operating-system lock without waiting; False if another process has it."""
    try:
        if os.name == 'nt':
            import msvcrt
            os.lseek(fd, _WINDOWS_REGION, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined]
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _unlock(fd: int) -> None:
    try:
        if os.name == 'nt':
            import msvcrt
            os.lseek(fd, _WINDOWS_REGION, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass


def _parse(text: str) -> dict | None:
    if not text.strip():
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _read_fd(fd: int) -> dict | None:
    os.lseek(fd, 0, os.SEEK_SET)
    chunks = []
    while chunk := os.read(fd, 65536):
        chunks.append(chunk)
    return _parse(b''.join(chunks).decode('utf-8', 'replace'))


def _write_fd(fd: int, text: str) -> None:
    os.lseek(fd, 0, os.SEEK_SET)
    os.ftruncate(fd, 0)
    os.write(fd, text.encode('utf-8'))
    os.fsync(fd)


def read_holder(path: Path) -> dict | None:
    """Who the lock file names: None when unlocked (no file, or empty),
    {} when its content cannot be read."""
    try:
        text = (Path(path) / LOCK_NAME).read_text(encoding='utf-8')
    except FileNotFoundError:
        return None
    except OSError:
        return {}
    return _parse(text)


def held_here(path: Path) -> bool:
    """True when a process on this computer holds the lock right now."""
    lock = Path(path) / LOCK_NAME
    if str(lock) in _held:
        return True
    try:
        fd = os.open(lock, os.O_RDWR)
    except OSError:
        return False
    try:
        if _try_lock(fd):
            _unlock(fd)
            return False
        return True
    finally:
        os.close(fd)


def acquire(path: Path, purpose: str) -> dict:
    """Take the lock in `path` or raise LockedError. Returns our holder record."""
    path = Path(path)
    # A new login data folder must stay private, as browser.launch makes it.
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = path / LOCK_NAME
    fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        if str(lock) in _held or not _try_lock(fd):
            raise LockedError(path, read_holder(path) or {}, removable=False, here=True)
        previous = _read_fd(fd)
        if previous and previous.get('host') not in (None, socket.gethostname()):
            # Written by another computer through a synced folder: it may be running.
            _unlock(fd)
            raise LockedError(path, previous, removable=True)
        # Anything else was left by a process of this computer that has ended.
        mine = {'host': socket.gethostname(), 'pid': os.getpid(), 'purpose': purpose,
                'started': time.strftime('%Y-%m-%d %H:%M:%S'), 'token': secrets.token_hex(8)}
        _write_fd(fd, json.dumps(mine))
    except BaseException:
        os.close(fd)
        raise
    _held[str(lock)] = (fd, mine['token'])
    return mine


def release(path: Path, mine: dict) -> None:
    """Give the lock back: empty the file, then release the operating-system lock."""
    entry = _held.pop(str(Path(path) / LOCK_NAME), None)
    if entry is None:
        return
    fd, token = entry
    try:
        if token == mine.get('token'):
            _write_fd(fd, '')
    except OSError:
        pass
    finally:
        _unlock(fd)
        os.close(fd)


def remove(path: Path) -> dict | None:
    """Clear a lock whose holder is not running on this computer (an explicit
    user action, for example after another computer crashed). Returns the
    holder that was cleared, or None when there was no lock. Raises
    LockedError when a process on this computer holds it."""
    holder = read_holder(path)
    if holder is None:
        return None
    lock = Path(path) / LOCK_NAME
    fd = os.open(lock, os.O_RDWR)
    try:
        if str(lock) in _held or not _try_lock(fd):
            raise LockedError(path, holder, removable=False, here=True)
        try:
            _write_fd(fd, '')
        finally:
            _unlock(fd)
    finally:
        os.close(fd)
    return holder


@contextmanager
def hold(path: Path, purpose: str):
    """`with hold(folder, 'update'):` runs the block with the lock held."""
    mine = acquire(path, purpose)
    try:
        yield mine
    finally:
        release(path, mine)
