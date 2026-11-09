"""Lock files: one run at a time per database and per login data folder.

The lock is a small JSON file (`.reclaim.lock`) naming the computer,
process and start time of the run that holds it. It works across
processes and, for a database in a synced folder, across computers.

A lock left by a process that no longer runs on this computer (a crash or
a killed window) is cleared automatically. A lock held by another
computer cannot be checked from here; the user removes it explicitly once
that computer is no longer running Reclaim My Chats.
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
# An unreadable lock this old is a leftover of a crash during creation.
_UNREADABLE_GRACE_S = 60


class LockedError(RuntimeError):
    """Another run holds the lock."""

    def __init__(self, path: Path, holder: dict, *, removable: bool):
        self.path = Path(path)
        self.holder = holder
        self.removable = removable  # True when the holder cannot be running here
        what = 'the login data folder' if holder.get('purpose') == 'browser' else 'the database'
        host = holder.get('host') or 'an unknown computer'
        since = holder.get('started') or 'an unknown time'
        if holder.get('host') == socket.gethostname():
            who = f'another Reclaim My Chats window or command on this computer (process {holder.get("pid")})'
        else:
            who = f'Reclaim My Chats on {host}'
        advice = ('If that run has ended, for example because the computer crashed, '
                  'remove the lock and try again.' if removable else
                  'Wait for it to finish, or close it, then try again.')
        super().__init__(f'{what[0].upper()}{what[1:]} is in use by {who} since {since}. {advice}')


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == 'nt':
        import ctypes
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return kernel32.GetLastError() == 5  # ERROR_ACCESS_DENIED: exists, not ours
        try:
            code = ctypes.c_ulong()
            kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def running_here(holder: dict) -> bool:
    """True when the holder is a process still running on this computer."""
    pid = holder.get('pid')
    return (holder.get('host') == socket.gethostname() and isinstance(pid, int)
            and _pid_alive(pid))


def read_holder(path: Path) -> dict | None:
    """The lock's holder, {} for an unreadable lock, None when unlocked."""
    try:
        text = (Path(path) / LOCK_NAME).read_text(encoding='utf-8')
    except FileNotFoundError:
        return None
    except OSError:
        return {}
    try:
        data = json.loads(text)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _stale(path: Path, holder: dict) -> bool:
    """True when the holder certainly no longer runs."""
    if not holder:
        try:
            age = time.time() - (Path(path) / LOCK_NAME).stat().st_mtime
        except OSError:
            return True
        return age > _UNREADABLE_GRACE_S
    if holder.get('host') != socket.gethostname():
        return False
    pid = holder.get('pid')
    return not isinstance(pid, int) or not _pid_alive(pid)


def acquire(path: Path, purpose: str) -> dict:
    """Take the lock in `path` or raise LockedError. Returns our holder record."""
    path = Path(path)
    # A new login data folder must stay private, as browser.launch makes it.
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    mine = {'host': socket.gethostname(), 'pid': os.getpid(), 'purpose': purpose,
            'started': time.strftime('%Y-%m-%d %H:%M:%S'), 'token': secrets.token_hex(8)}
    lock = path / LOCK_NAME
    for _ in range(3):
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            holder = read_holder(path)
            if holder is None:
                continue  # released meanwhile
            if _stale(path, holder):
                try:
                    lock.unlink()
                except FileNotFoundError:
                    pass
                continue
            other_host = holder.get('host') not in (None, socket.gethostname())
            raise LockedError(path, holder, removable=other_host or not holder) from None
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(mine, stream)
        return mine
    raise LockedError(path, read_holder(path) or {}, removable=True)


def release(path: Path, mine: dict) -> None:
    """Remove the lock if it is still ours."""
    holder = read_holder(path)
    if holder and holder.get('token') == mine.get('token'):
        try:
            (Path(path) / LOCK_NAME).unlink()
        except FileNotFoundError:
            pass


def remove(path: Path) -> dict | None:
    """Remove a lock regardless of its holder (an explicit user action).
    Returns the holder record that was removed, or None."""
    holder = read_holder(path)
    if holder is None:
        return None
    try:
        (Path(path) / LOCK_NAME).unlink()
    except FileNotFoundError:
        return None
    return holder


@contextmanager
def hold(path: Path, purpose: str):
    """`with hold(folder, 'update'):` runs the block with the lock held."""
    mine = acquire(path, purpose)
    try:
        yield mine
    finally:
        release(path, mine)
