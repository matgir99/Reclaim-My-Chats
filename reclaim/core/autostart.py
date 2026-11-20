"""Per-user autostart registration and desktop notifications."""

from __future__ import annotations

import contextlib
import importlib
import os
import shlex
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

APP_ID = 'org.reclaimmychats.app'
WINDOWS_RUN_KEY = r'Software\Microsoft\Windows\CurrentVersion\Run'
_LINUX_DISPLAY_VARS = ('DISPLAY', 'WAYLAND_DISPLAY', 'DBUS_SESSION_BUS_ADDRESS')


def _is_windows(sys_name: str) -> bool:
    return sys_name in ('win32', 'nt', 'windows')


def default_command(system: str | None = None) -> str:
    """Command used when registering start-at-login for this installation.

    Windows reads only double quotes around a path with spaces (the
    installer's own entry is `"...\\ReclaimMyChats.exe"`); the Linux and
    macOS entries are read with shell quoting."""
    args = [sys.executable] if getattr(sys, 'frozen', False) else [sys.executable, '-m', 'reclaim', 'gui']
    if _is_windows((system or sys.platform).lower()):
        return ' '.join([f'"{args[0]}"', *args[1:]])
    return ' '.join(shlex.quote(arg) for arg in args)


def linux_autostart_path(home: Path | None = None) -> Path:
    base = Path(home) if home is not None else Path.home()
    config_home = os.environ.get('XDG_CONFIG_HOME')
    root = Path(config_home) if (config_home and home is None) else (base / '.config')
    return root / 'autostart' / f'{APP_ID}.desktop'


def macos_plist_path(home: Path | None = None) -> Path:
    base = Path(home) if home is not None else Path.home()
    return base / 'Library' / 'LaunchAgents' / f'{APP_ID}.plist'


def _write_linux_entry(path: Path, enabled: bool, cmd: str) -> Path:
    if enabled:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            '[Desktop Entry]\n'
            'Type=Application\n'
            'Name=Reclaim My Chats\n'
            'Comment=Save and synchronize your AI conversations in a local database\n'
            f'Exec={cmd}\n'
            'Terminal=false\n'
            'X-GNOME-Autostart-enabled=true\n',
            encoding='utf-8',
        )
    elif path.exists():
        path.unlink()
    return path


def _write_macos_entry(path: Path, enabled: bool, cmd: str) -> Path:
    if enabled:
        path.parent.mkdir(parents=True, exist_ok=True)
        args = ''.join(f'    <string>{escape(part)}</string>\n' for part in shlex.split(cmd))
        path.write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
            '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
            '<plist version="1.0">\n'
            '<dict>\n'
            f'  <key>Label</key><string>{APP_ID}</string>\n'
            '  <key>ProgramArguments</key>\n'
            '  <array>\n'
            f'{args}'
            '  </array>\n'
            '  <key>RunAtLoad</key><true/>\n'
            '</dict>\n'
            '</plist>\n',
            encoding='utf-8',
        )
    elif path.exists():
        path.unlink()
    return path


def _windows_autostart(enabled: bool, cmd: str) -> None:
    winreg = importlib.import_module('winreg')
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, WINDOWS_RUN_KEY, 0,
                        winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, 'ReclaimMyChats', 0, winreg.REG_SZ, cmd)
        else:
            with contextlib.suppress(FileNotFoundError):
                winreg.DeleteValue(key, 'ReclaimMyChats')


def set_autostart(enabled: bool, *, command: str | None = None,
                  home: Path | None = None, system: str | None = None,
                  win_setter: Callable[[bool, str], None] | None = None) -> Path | str | None:
    """Create or remove a per-user login autostart entry on Linux, macOS, or Windows."""
    sys_name = (system or sys.platform).lower()
    cmd = command or default_command(sys_name)
    if sys_name.startswith('linux'):
        return _write_linux_entry(linux_autostart_path(home), enabled, cmd)
    if sys_name == 'darwin':
        return _write_macos_entry(macos_plist_path(home), enabled, cmd)
    if _is_windows(sys_name):
        (win_setter or _windows_autostart)(enabled, cmd)
        return WINDOWS_RUN_KEY
    return None


def _has_linux_display() -> bool:
    return any(os.environ.get(name) for name in _LINUX_DISPLAY_VARS)


def _notification_command(title: str, message: str, *,
                          sys_name: str, has_runner: bool) -> list[str] | None:
    if sys_name.startswith('linux'):
        if has_runner or (shutil.which('notify-send') and _has_linux_display()):
            return ['notify-send', title, message]
        return None
    if sys_name == 'darwin':
        safe_title = title.replace('"', '\\"')
        safe_msg = message.replace('"', '\\"')
        return ['osascript', '-e', f'display notification "{safe_msg}" with title "{safe_title}"']
    return None


def notify_desktop(title: str, message: str, *,
                   runner: Callable[[list[str]], Any] | None = None,
                   system: str | None = None) -> bool:
    """Send a best-effort desktop notification; never raises."""
    if os.environ.get('RECLAIM_NO_NOTIFY'):
        return False
    cmd = _notification_command(
        title, message,
        sys_name=(system or sys.platform).lower(),
        has_runner=runner is not None,
    )
    if cmd is None:
        return False
    with contextlib.suppress(Exception):
        if runner is not None:
            runner(cmd)
            return True
        completed = subprocess.run(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            check=False, timeout=2,
        )
        return completed.returncode == 0
    return False
