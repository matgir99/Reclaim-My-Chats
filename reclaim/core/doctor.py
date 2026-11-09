"""Diagnostics: is everything in order, and if not, what to do.

`run_checks` looks at this computer only: version, settings, database, locks,
browser, login data folder and log file. `live_checks` also logs in to each
enabled provider and lists its chats, writing nothing. The window's
Diagnostics panel and `reclaim doctor [--live]` show the same results, and
`report` turns them into text to copy into an issue.
"""

from __future__ import annotations

import os
import platform
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from . import browser, config, database, locks, logs, status
from .events import EventSink, ProgressEvent
from .render import TerminalRenderer, fanout

OK, INFO, WARNING, PROBLEM = 'ok', 'info', 'warning', 'problem'
_SYNCED_NAMES = {'Dropbox', 'OneDrive', 'Google Drive', 'iCloud Drive', 'Nextcloud', 'pCloud Drive'}


@dataclass(frozen=True)
class Check:
    area: str
    status: str            # ok, info, warning or problem
    detail: str
    advice: str = ''


def _version() -> str:
    from importlib.metadata import PackageNotFoundError, version
    try:
        return version('reclaimmychats')
    except PackageNotFoundError:
        return 'development checkout'


def _synced_folder(path: Path) -> str | None:
    """The sync tool a folder belongs to, if any can be recognised."""
    for folder in (path, *path.parents):
        if (folder / '.stfolder').exists():
            return 'Syncthing'
        if folder.name in _SYNCED_NAMES:
            return folder.name
    return None


def _settings_check(root: Path) -> tuple[Check, bool]:
    path = root / config.CONFIG_NAME
    try:
        data = config.load(root)
    except config.SettingsError as exc:
        return Check('Settings', PROBLEM, str(exc),
                     'Fix the file, or use Reset settings in the window.'), False
    providers = ', '.join(data.get('providers') or []) or 'all providers'
    where = str(path) if path.exists() else 'defaults (no settings file)'
    return Check('Settings', OK, f'{where} · {providers}'), True


def _database_checks(root: Path) -> list[Check]:
    path = config.archive_root(root)
    state = database.inspect(path)
    if state.kind == 'missing' and config.uses_default_database(root):
        return [Check('Database', INFO, f'{path} will be created by the first update.')]
    if not state.usable:
        return [Check('Database', PROBLEM, state.message(),
                      'Connect the drive or synced folder, or choose another folder in '
                      'Settings (reclaim init creates a new database).')]
    chats = sum(info.chats for info in status.scan(path))
    note = '' if state.kind == 'ready' else ' · made by an earlier version; the next update marks it'
    checks = [Check('Database', OK, f'{path} · {chats:,} chats{note}')]
    if not os.access(path, os.W_OK):
        checks.append(Check('Database', PROBLEM, f'{path} is read-only for this user.',
                            'Give your user write access to the folder.'))
    free = shutil.disk_usage(path).free
    if free < 1 << 30:
        checks.append(Check('Free space', WARNING, f'{free / (1 << 20):,.0f} MB free in {path}.',
                            'Free some space before a large update.'))
    return checks


def _lock_check(area: str, folder: Path) -> Check:
    holder = locks.read_holder(folder)
    if holder is None:
        return Check(area, OK, 'free')
    if locks.held_here(folder):
        return Check(area, INFO, f'in use by a run on this computer (process {holder.get("pid")})')
    return Check(area, WARNING, f'locked by {holder.get("host") or "an unknown computer"} '
                                f'since {holder.get("started") or "an unknown time"}',
                 'If that computer is no longer running Reclaim My Chats, use Remove lock '
                 'in the window or reclaim unlock.')


def _profile_checks(root: Path) -> list[Check]:
    profile = config.profile_root(root)
    if not profile.exists():
        return [Check('Login data', INFO, f'{profile} will be created by the first login.')]
    checks = [Check('Login data', OK, str(profile))]
    if os.name != 'nt' and profile.stat().st_mode & 0o077:
        checks.append(Check('Login data', WARNING, 'Other users of this computer can read it.',
                            f'Run: chmod 700 "{profile}"'))
    synced = _synced_folder(profile)
    if synced:
        checks.append(Check('Login data', WARNING,
                            f'It is inside a folder synced by {synced}.',
                            'Login sessions are stored with a fixed key; keep this folder out '
                            'of synced and shared locations.'))
    return checks


def run_checks(root: Path | None = None) -> list[Check]:
    """Everything that can be checked without contacting a provider."""
    root = Path(root) if root is not None else config.default_root()
    checks = [Check('Version', INFO, f'Reclaim My Chats {_version()} · Python '
                                     f'{platform.python_version()} · {platform.platform(terse=True)}')]
    settings, usable = _settings_check(root)
    checks.append(settings)
    if usable:
        checks += _database_checks(root)
        checks.append(_lock_check('Database lock', config.archive_root(root)))
        checks += _profile_checks(root)
        checks.append(_lock_check('Login data lock', config.profile_root(root)))
    else:
        checks += [Check('Database', INFO, 'Not checked until the settings are fixed.'),
                   Check('Login data', INFO, 'Not checked until the settings are fixed.')]
    chrome = browser.find_chrome()
    checks.append(Check('Browser', OK, chrome) if chrome else
                  Check('Browser', WARNING, 'No Chrome or Chromium was found.',
                        'Install Google Chrome or Chromium, or set RECLAIM_CHROME_PATH.'))
    log_dir = logs.log_dir(root)
    writable = os.access(log_dir if log_dir.exists() else root, os.W_OK)
    checks.append(Check('Log file', OK if writable else WARNING, str(logs.log_file(root)),
                        '' if writable else 'The data folder is read-only; runs are not logged.'))
    return checks


def live_checks(root: Path | None = None, providers: list[str] | None = None,
                controller=None, echo: EventSink | None = None) -> list[Check]:
    """Log in to each provider and list its chats, writing nothing. `echo`
    also receives the run's events, for example to show login requests."""
    from .service import ALL_PROVIDERS, ArchiveController
    root = Path(root) if root is not None else config.default_root()
    if providers is None:
        providers, _, _ = config.selected_providers(root, ALL_PROVIDERS)
    events: list[ProgressEvent] = []
    (controller or ArchiveController(root)).run(providers, 'check',
                                                fanout(events.append, echo))
    return checks_from_events(events)


def checks_from_events(events: list[ProgressEvent]) -> list[Check]:
    """Live-check results from the run's events: one result per provider."""
    results: dict[str, Check] = {}
    for event in events:
        if event.kind == 'checked':
            results[event.provider] = Check(event.provider, OK, event.message)
        elif event.kind == 'error' and event.provider:
            results[event.provider] = Check(event.provider, PROBLEM, event.message,
                                            'Log in again in the window, then check again; '
                                            'if it keeps failing, the service may have changed.')
    return list(results.values())


def report(checks: list[Check], live: list[Check] | None = None) -> str:
    """Plain text for an issue report. Contains paths, never chat content."""
    width = max(len(c.area) for c in [*checks, *(live or [])])
    lines = ['Reclaim My Chats diagnostics', '']
    for check in [*checks, *(live or [])]:
        lines.append(f'{check.status.upper():8} {check.area:<{width}}  {check.detail}')
        if check.advice:
            lines.append(f'{"":8} {"":<{width}}  → {check.advice}')
    return '\n'.join(lines) + '\n'


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog='reclaim doctor',
                                 description='Check settings, database, locks, browser and '
                                             'login data; --live also logs in to each '
                                             'provider and lists its chats, writing nothing.')
    ap.add_argument('--live', action='store_true',
                    help='also log in to each enabled provider and list its chats')
    args = ap.parse_args(argv)
    checks = run_checks()
    live = live_checks(echo=TerminalRenderer(sys.stdout)) if args.live else None
    sys.stdout.write(report(checks, live))
    return 1 if any(c.status == PROBLEM for c in [*checks, *(live or [])]) else 0
