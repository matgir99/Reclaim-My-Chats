"""Flet desktop frontend for native database updates."""

from __future__ import annotations

import asyncio
import contextlib
import os
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    import flet as ft
else:
    try:
        import flet as ft
    except ImportError:  # The CLI intentionally has no GUI dependency.
        ft = None

from .core import autostart, browser, config, database, doctor, lifecycle, locks, logs, releases, render, status, tidy
from .core.events import EventSink, ProgressEvent
from .core.service import ALL_PROVIDERS, ArchiveController, BusyOperationError, RunSummary

DISPLAY_NAMES = {
    'googleaistudio': 'Google AI Studio',
    'deepseek': 'DeepSeek',
    'kimi': 'Kimi',
    'chatgpt': 'ChatGPT',
    'claude': 'Claude',
    'googlegemini': 'Google Gemini',
}
# Events about one chat: they move the progress bar and the status line.
CHAT_PROGRESS = frozenset({'chat_started', 'chat_skipped', 'chat_completed',
                           'chat_planned', 'error'})


class Controller(Protocol):
    @property
    def busy(self) -> bool: ...

    def run(self, providers: list[str], mode: str = 'update',
            sink: EventSink | None = None, *, log: bool = False,
            only_ids: list[str] | None = None) -> RunSummary: ...

    def capture(self, provider: str, url: str, sink: EventSink | None = None,
                destination: Path | None = None) -> RunSummary: ...

    def stop(self) -> None: ...

    def bring_browser_forward(self) -> bool: ...


def configured_providers(root: Path) -> list[str]:
    chosen, _, _ = config.selected_providers(root, ALL_PROVIDERS)
    return chosen


def open_directory(path: Path) -> None:
    if not path.is_dir():
        raise FileNotFoundError(f'Database folder does not exist yet: {path}')
    if os.name == 'nt':
        os.startfile(str(path))
    elif sys.platform == 'darwin':
        subprocess.Popen(['open', str(path)])
    else:
        subprocess.Popen(['xdg-open', str(path)])


def status_text(info: status.ProviderStatus) -> str:
    if not info.present:
        return (f'No chats saved yet · last checked {info.last_checked}'
                if info.last_checked else 'No chats saved yet')
    summary = f'{info.chats} chats in database'
    total_distinct = info.chats + info.archived_chats + info.deleted_chats
    if info.chat_folders > total_distinct:
        summary += f' ({info.chat_folders - total_distinct} older copies)'
    if info.archived_chats:
        summary += f' · {info.archived_chats} archived'
    if info.deleted_chats:
        summary += f' · {info.deleted_chats} deleted'
    if info.last_sync:
        summary += f' · last download {info.last_sync}'
    if info.last_checked:
        summary += f' · last checked {info.last_checked}'
    if info.last_run:
        summary += (f' · last run {info.last_run["ok"]} ok, '
                    f'{info.last_run["failed"]} failed')
    if info.last_error:
        summary += f' · last attempt failed: {info.last_error}'
    return summary


def _review_items(events: list[ProgressEvent]) -> tuple[list[tuple[ProgressEvent, str]], list[str]]:
    """Combine planned fetches and lifecycle moves per chat and collect brake warnings."""
    by_key: dict[tuple[str, str], tuple[ProgressEvent, list[str]]] = {}
    warnings: list[str] = []
    for event in events:
        if event.kind == 'warning' and 'Mass-change brake' in event.message:
            name = DISPLAY_NAMES.get(event.provider, event.provider)
            warnings.append(f'{name}: {event.message}' if name else event.message)
        elif event.kind == 'chat_planned' and event.message.startswith(('fetch', 'move')):
            key = (event.provider, event.detail or event.title)
            if key in by_key:
                by_key[key][1].append(event.message)
            else:
                by_key[key] = (event, [event.message])
    return [(ev, ' + '.join(acts)) for ev, acts in by_key.values()], warnings


class ReclaimWindow:
    def __init__(self, page, root: Path | None = None,
                 controller: Controller | None = None):
        if ft is None:
            raise RuntimeError('Flet is required for the GUI')
        self.page = page
        self.root = Path(root) if root is not None else config.default_root()
        self.controller = controller or ArchiveController(self.root)
        self.settings_error: config.SettingsError | None = None
        try:
            self.enabled = configured_providers(self.root)
        except config.SettingsError as exc:
            self.settings_error = exc
            self.enabled = []
        self.blocked = False  # a settings or database problem stops runs
        self._warnings = 0
        self._run_events: list[ProgressEvent] = []
        self._live_results: list[doctor.Check] = []
        self._captured_path: Path | None = None
        self._capture_provider = self.enabled[0] if self.enabled else ALL_PROVIDERS[0]
        self._capture_url = ''
        self._running = False
        self._stopping = False
        self._log_lines: list[str] = []
        self._provider_index = 0
        self._provider_count = 1
        self._build()

    def _build(self) -> None:
        """The main window, top to bottom: title with Diagnostics and Settings,
        the problem banner, one row per provider with the run buttons, the
        progress section, and the database section with Details (the run's
        messages). Controls that tests or code look up carry a `key`."""
        page = self.page
        page.title = 'Reclaim My Chats'
        page.window.width = 820
        page.window.height = 650
        page.window.min_width = 650
        page.window.min_height = 550
        page.scroll = ft.ScrollMode.AUTO

        self.archive_path = ft.Text(str(self._database_path() or ''),
                                    selectable=True, key='archive-path')
        self.archive_total = ft.Text('Loading database status', key='archive-total')
        self.current = ft.Text('Ready', key='current-operation')
        self.summary = ft.Text('', key='completion-summary')
        self.progress = ft.ProgressBar(value=0, key='run-progress')
        self.log = ft.TextField(value='', multiline=True, read_only=True,
                                min_lines=6, max_lines=10, key='details-log')

        self.checks = {}
        self.provider_status = {}
        self.provider_chips = {}
        self.provider_login_buttons = {}
        self.provider_review_buttons = {}
        rows = []
        for name in ALL_PROVIDERS:
            enabled = name in self.enabled
            check = ft.Checkbox(label=DISPLAY_NAMES[name], value=enabled,
                                disabled=not enabled, on_change=self._selection_changed,
                                key=f'provider-{name}')
            chip = ft.Text('Ready' if enabled else 'Disabled', size=12,
                           weight=ft.FontWeight.BOLD, key=f'provider-chip-{name}')
            chip_box = ft.Container(content=chip, bgcolor=ft.Colors.SURFACE_CONTAINER_HIGHEST,
                                    padding=6, border_radius=12)
            text = ft.Text('Loading' if enabled else 'Disabled in settings',
                           key=f'provider-status-{name}', expand=True)
            login_btn = ft.TextButton('Log in', on_click=self._make_provider_action(name, 'check'),
                                      key=f'provider-login-{name}', disabled=not enabled)
            review_btn = ft.TextButton('Review', on_click=self._make_provider_action(name, 'dry-run'),
                                       key=f'provider-review-{name}', disabled=not enabled)
            self.checks[name] = check
            self.provider_status[name] = text
            self.provider_chips[name] = chip
            self.provider_login_buttons[name] = login_btn
            self.provider_review_buttons[name] = review_btn
            rows.append(ft.Row([check, chip_box, text, login_btn, review_btn],
                               alignment=ft.MainAxisAlignment.SPACE_BETWEEN))

        self.update_selected = ft.FilledButton('Update selected',
                                               on_click=self._update_selected,
                                               key='update-selected')
        self.update_all = ft.Button('Update all', on_click=self._update_all,
                                    key='update-all')
        self.stop_button = ft.OutlinedButton('Stop', on_click=self._stop_run,
                                             key='stop-run', disabled=True)
        self.dry_run = ft.Button('Check for updates', on_click=self._dry_run,
                                 key='dry-run')
        self.rebuild = ft.OutlinedButton('Download everything again',
                                         on_click=self._confirm_rebuild,
                                         key='rebuild')
        self.open_archive = ft.Button('Open database folder', on_click=self._open_archive,
                                      key='open-archive')
        self.open_logs = ft.Button('Open log folder', on_click=self._open_logs,
                                   key='open-logs')
        self.tidy_button = ft.Button('Tidy duplicates', on_click=self._tidy_dialog,
                                     key='tidy-database')
        self.review_states_button = ft.Button('Archived & deleted',
                                              on_click=self._archived_deleted_dialog,
                                              key='review-archived-deleted')
        ver = releases.app_version()
        self.version_text = ft.Text(ver if ver == 'dev' else f'v{ver}',
                                    color=ft.Colors.ON_SURFACE_VARIANT, key='app-version')
        self.release_notice_text = ft.Text('', selectable=True, key='release-notice-text',
                                           color=ft.Colors.ON_SECONDARY_CONTAINER)
        self.release_notice = ft.Container(
            content=self.release_notice_text,
            bgcolor=ft.Colors.SECONDARY_CONTAINER, padding=10, border_radius=8,
            visible=False, key='release-notice')
        self.setup_banner_text = ft.Text(
            'New here or moving your database? Run First-run setup to choose providers, '
            'folders, and browser.',
            color=ft.Colors.ON_PRIMARY_CONTAINER, key='setup-banner-text', expand=True)
        self.setup_banner = ft.Container(
            content=ft.Row([
                self.setup_banner_text,
                ft.FilledButton('First-run setup', on_click=self._setup_wizard,
                                key='start-setup'),
            ], alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
            bgcolor=ft.Colors.PRIMARY_CONTAINER, padding=12, border_radius=8,
            visible=False, key='setup-banner')
        self.notice_text = ft.Text('', selectable=True, key='notice-text',
                                   color=ft.Colors.ON_ERROR_CONTAINER)
        self.notice_actions = ft.Row([], wrap=True, key='notice-actions')
        self.notice = ft.Container(
            content=ft.Column([self.notice_text, self.notice_actions], spacing=10),
            bgcolor=ft.Colors.ERROR_CONTAINER, padding=14, border_radius=8,
            visible=False, key='notice')
        self.login_banner_text = ft.Text('', selectable=True, key='login-banner-text',
                                         color=ft.Colors.ON_TERTIARY_CONTAINER, expand=True)
        self.bring_browser_button = ft.FilledButton('Bring browser forward',
                                                    on_click=self._bring_browser,
                                                    key='bring-browser-forward')
        self.login_banner = ft.Container(
            content=ft.Row([self.login_banner_text, self.bring_browser_button],
                           alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
            bgcolor=ft.Colors.TERTIARY_CONTAINER, padding=14, border_radius=8,
            visible=False, key='login-banner')
        self.setup_button = ft.Button('Setup', on_click=self._setup_wizard, key='setup-wizard')
        self.settings_button = ft.Button('Settings', on_click=self._settings,
                                         key='settings')
        self.diagnostics_button = ft.Button('Diagnostics', on_click=self._diagnostics,
                                            key='diagnostics')

        page.add(ft.Container(
            padding=20,
            content=ft.Column([
                ft.Row([
                    ft.Row([
                        ft.Text('Reclaim My Chats', size=28, weight=ft.FontWeight.BOLD),
                        self.version_text,
                    ], vertical_alignment=ft.CrossAxisAlignment.CENTER, spacing=10),
                    ft.Row([self.setup_button, self.diagnostics_button,
                            self.settings_button], wrap=True),
                ], alignment=ft.MainAxisAlignment.SPACE_BETWEEN, wrap=True),
                ft.Text('Save and synchronize your AI conversations in a local database'),
                self.release_notice,
                self.setup_banner,
                self.notice,
                self.login_banner,
                ft.Divider(),
                ft.Text('Providers', size=20, weight=ft.FontWeight.BOLD),
                *rows,
                ft.Row([self.update_selected, self.update_all, self.stop_button], wrap=True),
                ft.Row([self.dry_run, self.rebuild], wrap=True),
                ft.Divider(),
                ft.Text('Progress', size=20, weight=ft.FontWeight.BOLD),
                self.progress,
                self.current,
                self.summary,
                ft.Divider(),
                ft.Text('Database', size=20, weight=ft.FontWeight.BOLD),
                self.archive_total,
                self.archive_path,
                ft.Row([self.open_archive, self.open_logs, self.tidy_button,
                        self.review_states_button], wrap=True),
                ft.ExpansionTile(title='Details', controls=[self.log],
                                 key='details'),
            ], spacing=12),
        ))
        self._selection_changed()

    def _database_path(self) -> Path | None:
        """The database folder, or None while the settings file is invalid."""
        try:
            return config.archive_root(self.root)
        except config.SettingsError as exc:
            self.settings_error = exc
            return None

    async def refresh_archive(self, *, preserve_prefixes: tuple[str, ...] = ()) -> None:
        """Re-read settings and database state, then the saved chat counts."""
        archive = self._database_path()
        if archive is None or self.settings_error is not None:
            assert self.settings_error is not None
            self._show_settings_problem(self.settings_error)
            self.page.update()
            return
        self.archive_path.value = str(archive)
        try:
            state = await asyncio.to_thread(database.inspect, archive)
            infos = await asyncio.to_thread(status.scan, archive, checks_root=self.root)
        except Exception as exc:
            self._error(f'Could not read database status: {exc}')
            return
        first_use = state.kind in ('missing', 'empty') and config.uses_default_database(self.root)
        if state.usable or first_use:
            self._hide_notice()
        else:
            self._show_database_problem(state)
        self.setup_banner.visible = config.needs_first_run_setup(self.root) and not self.blocked
        self._refresh_provider_rows(infos, preserve_prefixes)
        self.page.update()

    def _refresh_provider_rows(self, infos: list[status.ProviderStatus],
                               preserve_prefixes: tuple[str, ...]) -> None:
        self.archive_total.value = (
            f'{sum(i.chats + i.archived_chats + i.deleted_chats for i in infos):,} '
            'chats in the database'
        )
        for info in infos:
            current = str(self.provider_status[info.provider].value or '')
            if preserve_prefixes and current.startswith(preserve_prefixes):
                continue
            if info.provider in self.enabled:
                self.provider_status[info.provider].value = status_text(info)
                self._set_chip(info.provider, 'Error' if info.last_error else 'Ready')
            else:
                self.provider_status[info.provider].value = 'Disabled in settings'
                self._set_chip(info.provider, 'Disabled')

    def _set_chip(self, provider: str, state: str) -> None:
        chip = self.provider_chips.get(provider)
        if chip is not None:
            chip.value = state

    def _make_provider_action(self, provider: str, mode: str):
        async def act(_=None) -> None:
            await self._run([provider], mode)
        return act

    def _bring_browser(self, _=None) -> None:
        self.controller.bring_browser_forward()

    async def check_release_update(self, *, fetcher=None) -> None:
        """Check non-blockingly for a newer GitHub release and show a notice if found."""
        info = await asyncio.to_thread(releases.check_for_update, fetcher=fetcher)
        if info is not None:
            self.release_notice_text.value = f'New release available: v{info.version} ({info.url})'
            self.release_notice.visible = True
            self.page.update()

    # Problems that stop runs, shown in the banner under the title

    def _show_notice(self, text: str, actions: list) -> None:
        self.blocked = True
        self.notice_text.value = text
        self.notice_actions.controls = actions
        self.notice.visible = True
        self._selection_changed()

    def _hide_notice(self) -> None:
        self.blocked = False
        self.notice.visible = False
        self.notice_actions.controls = []
        self._selection_changed()

    def _show_settings_problem(self, exc: config.SettingsError) -> None:
        self._show_notice(str(exc), [
            ft.Button('Open settings folder', key='notice-open-settings',
                      on_click=lambda _: self._open(exc.path.parent)),
            ft.FilledButton('Reset settings', key='notice-reset-settings',
                            on_click=self._reset_settings),
        ])

    def _show_database_problem(self, state: database.DatabaseState) -> None:
        actions = []
        if state.kind == 'missing':
            actions.append(ft.FilledButton('Retry', key='notice-retry',
                                           on_click=self._retry))
        if state.kind in ('missing', 'empty', 'foreign', 'file'):
            actions.append(ft.Button('Choose folder', key='notice-choose-folder',
                                     on_click=self._settings))
        if state.kind in ('missing', 'empty'):
            actions.append(ft.Button('Create database here', key='notice-create-database',
                                     on_click=lambda _: self._confirm_create(state.path)))
        self._show_notice(state.message(), actions)

    async def _retry(self, _=None) -> None:
        await self.refresh_archive()

    async def _reset_settings(self, _=None) -> None:
        try:
            backup = config.reset(self.root)
        except OSError as exc:
            self._error(f'Could not reset the settings: {exc}')
            return
        self.settings_error = None
        self._reload_providers()
        await self.refresh_archive()
        if backup is not None:
            self.summary.value = (f'Settings reset to the defaults. The old file was kept as '
                                  f'{backup.name}.')
            self.page.update()

    def _reload_providers(self) -> None:
        try:
            self.enabled = configured_providers(self.root)
        except config.SettingsError as exc:
            self.settings_error = exc
            self.enabled = []
        for name in ALL_PROVIDERS:
            enabled = name in self.enabled
            self.checks[name].disabled = not enabled
            self.checks[name].value = enabled
            if not enabled:
                self._set_chip(name, 'Disabled')
        self._selection_changed()

    def _confirm_create(self, path: Path, then=None) -> None:
        """Ask before creating a database, then run `then` (async) if given."""
        async def create(_=None) -> None:
            self.page.pop_dialog()
            try:
                await asyncio.to_thread(database.init, path)
            except (database.DatabaseError, OSError) as exc:
                self._error(str(exc))
                return
            if then is not None:
                await then()
            await self.refresh_archive()

        self.page.show_dialog(ft.AlertDialog(
            modal=True,
            title='Create a new database?',
            content=ft.Text(f'This creates an empty Reclaim My Chats database in {path}. '
                            'If your existing database is on a drive or in a synced folder '
                            'that is not connected, cancel, connect it, and press Retry.'),
            actions=[ft.OutlinedButton('Cancel', on_click=lambda _: self.page.pop_dialog(),
                                       key='cancel-create-database'),
                     ft.FilledButton('Create database', on_click=create,
                                     key='confirm-create-database')]))

    def _lock_dialog(self, exc: locks.LockedError) -> None:
        def remove(_=None) -> None:
            self.page.pop_dialog()
            try:
                locks.remove(exc.path)
            except OSError as error:
                self._error(f'Could not remove the lock: {error}')
                return
            self.summary.value = 'Lock removed. Start the update again.'
            self.page.update()

        actions: list[ft.Control] = [
            ft.OutlinedButton('Close', on_click=lambda _: self.page.pop_dialog(), key='close-lock')]
        if exc.removable:
            actions.append(ft.FilledButton('Remove lock', on_click=remove, key='remove-lock'))
        self.page.show_dialog(ft.AlertDialog(modal=True, title='Another run is using it',
                                             content=ft.Text(str(exc)), actions=actions))

    def _selected(self) -> list[str]:
        return [name for name in ALL_PROVIDERS
                if name in self.enabled and self.checks[name].value]

    def _selection_changed(self, _=None) -> None:
        selected = bool(self._selected())
        busy = self._running or self.blocked
        self.update_selected.disabled = busy or not selected
        self.dry_run.disabled = busy or not selected
        self.rebuild.disabled = busy or not selected
        self.update_all.disabled = busy or not self.enabled
        self.stop_button.disabled = not self._running or self._stopping
        for name in ALL_PROVIDERS:
            disabled_row = busy or name not in self.enabled
            self.provider_login_buttons[name].disabled = disabled_row
            self.provider_review_buttons[name].disabled = disabled_row
        # Settings cannot be edited while the settings file itself is broken.
        self.settings_button.disabled = self._running or self.settings_error is not None
        self.setup_button.disabled = self._running
        self.tidy_button.disabled = busy
        self.review_states_button.disabled = busy
        self.page.update()

    def _set_running(self, running: bool) -> None:
        self._running = running
        if not running:
            self._stopping = False
            self.login_banner.visible = False
        self._selection_changed()

    def _stop_run(self, _=None) -> None:
        if not self._running or self._stopping:
            return
        self._stopping = True
        self.stop_button.disabled = True
        self.current.value = 'Stopping after current chat...'
        self.controller.stop()
        self.page.update()

    def _append_log(self, line: str) -> None:
        self._log_lines.append(line)
        self._log_lines = self._log_lines[-200:]
        self.log.value = '\n'.join(self._log_lines)

    def _set_status(self, provider: str, text: str) -> None:
        control = self.provider_status.get(provider)
        if control is not None:
            control.value = text

    def _event(self, event: ProgressEvent) -> None:
        """The window's rendering of a run's events: the progress bar, the
        status line, each provider's row, and Details (in the same words as
        the log file, from render.describe)."""
        name = DISPLAY_NAMES.get(event.provider, event.provider)
        self._show_progress(event)
        self._show_status_line(event, name)
        row = self._row_status(event)
        if row is not None:
            self._set_status(event.provider, row)
        self._update_chip_for_event(event)
        self._update_login_banner(event, name)
        if event.kind == 'warning':
            self._warnings += 1
        if event.kind == 'captured' and event.detail:
            self._captured_path = Path(event.detail)
        line = render.describe(event)
        if line:
            self._append_log(f'{name}: {line}' if event.provider else line)
        self.page.update()

    def _update_login_banner(self, event: ProgressEvent, name: str) -> None:
        if event.kind == 'login_needed':
            self.login_banner_text.value = f'{name}: {event.message}'
            self.login_banner.visible = True
        elif event.kind in ('logged_in', 'provider_completed', 'run_completed', 'error', 'stopped'):
            self.login_banner.visible = False

    def _update_chip_for_event(self, event: ProgressEvent) -> None:
        label = {'provider_started': 'Running', 'logged_in': 'Running',
                 'login_needed': 'Needs login', 'error': 'Error',
                 'checked': 'Logged in', 'stopped': 'Stopped'}.get(event.kind)
        if label is not None:
            self._set_chip(event.provider, label)
        elif event.kind == 'provider_completed' and event.message == 'exit 0':
            chip = self.provider_chips.get(event.provider)
            if chip is not None and chip.value not in ('Logged in', 'Stopped'):
                self._set_chip(event.provider, 'Ready')

    def _show_progress(self, event: ProgressEvent) -> None:
        """The progress bar: each provider is an equal share of the run."""
        kind = event.kind
        if kind == 'run_started':
            self.progress.value = 0
        elif kind == 'provider_started':
            self._provider_index, self._provider_count = event.current, event.total
        elif kind in CHAT_PROGRESS and event.total:
            done = event.current - (1 if kind == 'chat_started' else 0)
            self.progress.value = min(1, ((self._provider_index - 1) + done / event.total)
                                      / self._provider_count)
        elif kind == 'provider_completed':
            self.progress.value = event.current / event.total
        elif kind == 'run_completed':
            self.progress.value = 1

    def _show_status_line(self, event: ProgressEvent, name: str) -> None:
        """The line under the buttons: what the run is doing now."""
        kind = event.kind
        if kind == 'run_started':
            self.current.value = f'Starting {event.message}'
        elif kind == 'provider_started':
            self.current.value = f'{name}: starting'
        elif kind in ('login_needed', 'listing', 'listed', 'captured', 'stopped'):
            self.current.value = f'{name}: {event.message}'
        elif kind in CHAT_PROGRESS and event.title:
            self.current.value = f'{name}: {event.title}'

    def _row_status(self, event: ProgressEvent) -> str | None:
        """The new text of the provider's row, or None to leave it."""
        kind = event.kind
        if kind in ('provider_started', 'logged_in'):
            return 'Running'
        if kind == 'login_needed':
            return 'Waiting for login in the browser window'
        prefixes = {'error': 'Error', 'checked': 'Live check', 'captured': 'Capture'}
        if kind in prefixes:
            return f'{prefixes[kind]}: {event.message}'
        if kind == 'stopped':
            return event.message or 'Stopped'
        if kind != 'provider_completed':
            return None
        row = self.provider_status.get(event.provider)
        current = str(row.value) if row is not None else ''
        if event.message == 'exit 0':
            return None if current.startswith(('Live check:', 'Capture:', 'Stopped')) else 'Completed'
        if current.startswith('Error:'):
            return None  # keep the error, which says more than the exit code
        return f'Failed ({event.message})'

    def _cannot_start(self, providers: list[str]) -> str | None:
        """Why a run cannot start now, or None when it can."""
        if not providers:
            return 'Select at least one enabled provider.'
        if self._running or self.controller.busy:
            return 'Another operation is already running.'
        if self.blocked:
            return 'Fix the problem shown at the top of the window first.'
        return None

    async def run_startup_check_if_enabled(self) -> None:
        """Run a non-blocking update check on startup when opted in by settings."""
        if not self.blocked and self.enabled and config.check_on_startup(self.root):
            with browser.no_login_wait():
                await self._run(list(self.enabled), 'dry-run')

    async def _run(self, providers: list[str], mode: str, *, url: str = '',
                   only_ids: list[str] | None = None) -> None:
        problem = self._cannot_start(providers)
        if problem:
            self._error(problem)
            return
        self._set_running(True)
        self.summary.value = ''
        self._warnings = 0
        if mode == 'capture':
            self._captured_path = None
        self._run_events = []
        self._log_lines.clear()
        self.log.value = ''
        self._provider_count = len(providers)
        completed_ok = False
        try:
            self._show_result(await self._execute(providers, mode, url=url, only_ids=only_ids), mode)
            completed_ok = True
        except BusyOperationError as exc:
            self._error(str(exc))
        except locks.LockedError as exc:
            self.current.value = 'Ready'
            self._lock_dialog(exc)
        except database.DatabaseError as exc:
            self.current.value = 'Ready'
            self._show_database_problem(database.inspect(exc.path))
        except config.SettingsError as exc:
            self.current.value = 'Ready'
            self.settings_error = exc
            self._show_settings_problem(exc)
        except Exception as exc:
            self.summary.value = f'Failed: {exc}'
            self._append_log(f'Run failed: {exc}')
            self._error(str(exc))
        finally:
            self._set_running(False)
            if mode != 'capture' and not self.blocked:
                await self.refresh_archive(preserve_prefixes=('Live check:', 'Capture:'))
            self.page.update()
        if completed_ok and mode == 'dry-run':
            self._maybe_show_review_dialog(providers)

    async def _execute(self, providers: list[str], mode: str, *, url: str = '',
                       only_ids: list[str] | None = None) -> RunSummary:
        """Run in a worker thread, showing its events as they arrive."""
        queue: asyncio.Queue[ProgressEvent | None] = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def receive(event: ProgressEvent) -> None:
            loop.call_soon_threadsafe(queue.put_nowait, event)

        def execute() -> RunSummary:
            try:
                if mode == 'capture':
                    return self.controller.capture(providers[0], url, receive)
                if only_ids is not None:
                    return self.controller.run(providers, mode, receive, only_ids=only_ids)
                return self.controller.run(providers, mode, receive)
            finally:
                # Use the same thread-safe channel as events so completion
                # cannot overtake progress/error messages still in transit.
                loop.call_soon_threadsafe(queue.put_nowait, None)

        task = asyncio.create_task(asyncio.to_thread(execute))
        while (event := await queue.get()) is not None:
            self._run_events.append(event)
            self._event(event)
        return await task

    def _maybe_show_review_dialog(self, providers: list[str]) -> None:
        planned, warnings = _review_items(self._run_events)
        if planned:
            self._review_plan_dialog(providers, planned, warnings)

    def _review_plan_dialog(self, providers: list[str],
                            planned: list[tuple[ProgressEvent, str]],
                            warnings: list[str] | None = None) -> None:
        """Show the planned downloads and moves per provider to merge all, subset, or skip."""
        brake_warnings = warnings or []
        items = [
            (e, ft.Checkbox(label=f'{DISPLAY_NAMES.get(e.provider, e.provider)}: '
                                  f'{e.title} ({action})',
                            value=True, key=f'plan-chat-{i}'))
            for i, (e, action) in enumerate(planned)
        ]

        async def merge_selected(_=None) -> None:
            self.page.pop_dialog()
            chosen_provs = list(dict.fromkeys(e.provider for e, c in items if c.value))
            chosen_ids = [e.detail or e.title for e, c in items if c.value]
            if chosen_provs and chosen_ids:
                await self._run(chosen_provs, 'update', only_ids=chosen_ids)

        async def merge_all(_=None) -> None:
            self.page.pop_dialog()
            if brake_warnings:
                all_ids = [e.detail or e.title for e, _ in items]
                await self._run(providers, 'update', only_ids=all_ids)
            else:
                await self._run(providers, 'update')

        controls: list[ft.Control] = [
            ft.Text(msg, color=ft.Colors.ERROR, weight=ft.FontWeight.BOLD,
                    selectable=True, key=f'review-brake-{i}')
            for i, msg in enumerate(brake_warnings)
        ]
        controls.extend(c for _, c in items)
        self.page.show_dialog(ft.AlertDialog(
            modal=True,
            key='review-plan-dialog',
            title='Review updates',
            content=ft.Container(
                width=580,
                content=ft.Column(controls, spacing=6,
                                  scroll=ft.ScrollMode.AUTO, height=340),
            ),
            actions=[
                ft.OutlinedButton('Skip', on_click=lambda _: self.page.pop_dialog(),
                                  key='review-skip'),
                ft.Button('Merge selected', on_click=merge_selected,
                          key='review-merge-selected'),
                ft.FilledButton('Merge all', on_click=merge_all, key='review-merge-all'),
            ],
        ))

    def _show_result(self, result: RunSummary, mode: str) -> None:
        labels = ', '.join(f'{DISPLAY_NAMES[name]}: {"ok" if code == 0 else "failed"}'
                           for name, code in result.codes)
        action = {'update': 'Update', 'rebuild': 'Rebuild',
                  'dry-run': 'Dry run', 'check': 'Live check', 'capture': 'Capture'}[mode]
        if mode == 'check':
            self._live_results = doctor.checks_from_events(self._run_events)
        stopped = self._stopping or any(e.kind == 'stopped' for e in self._run_events)
        if result.exit_code != 0:
            state = 'completed with errors'
        elif stopped:
            state = 'stopped'
        else:
            state = 'completed successfully'
        summary = f'{action} {state} · {labels}'
        if mode == 'capture' and self._captured_path is not None:
            summary += f' · {self._captured_path}'
        if self._warnings:
            plural = 's' if self._warnings != 1 else ''
            summary += f' · {self._warnings} warning{plural} (see Details)'
        self.summary.value = summary
        self.current.value = 'Ready'
        if mode in ('update', 'rebuild'):
            autostart.notify_desktop('Reclaim My Chats', summary)

    async def _update_selected(self, _=None) -> None:
        await self._run(self._selected(), 'update')

    async def _update_all(self, _=None) -> None:
        await self._run(list(self.enabled), 'update')

    async def _dry_run(self, _=None) -> None:
        await self._run(self._selected(), 'dry-run')

    def _confirm_rebuild(self, _=None) -> None:
        names = ', '.join(DISPLAY_NAMES[p] for p in self._selected())

        async def confirm(_=None) -> None:
            self.page.pop_dialog()
            await self._run(self._selected(), 'rebuild')

        dialog = ft.AlertDialog(
            modal=True,
            title='Rebuild selected providers?',
            content=ft.Text(f'This fetches every conversation again and overwrites its copy in the database for: {names}.'),
            actions=[ft.OutlinedButton('Cancel', on_click=lambda _: self.page.pop_dialog(),
                                       key='cancel-rebuild'),
                     ft.FilledButton('Rebuild', on_click=confirm, key='confirm-rebuild')],
        )
        self.page.show_dialog(dialog)

    # Diagnostics: the same checks as `reclaim doctor [--live]`

    def _capture_controls(self) -> list[ft.Control]:
        """The 'Capture a chat' section of the Diagnostics dialog."""
        provider_field = ft.Dropdown(
            label='Provider', value=self._capture_provider, width=210,
            options=[ft.DropdownOption(key=name, text=DISPLAY_NAMES[name])
                     for name in ALL_PROVIDERS],
            key='capture-provider')
        url_field = ft.TextField(label='Chat URL', value=self._capture_url,
                                 expand=True, key='capture-url')

        async def capture_chat(_=None) -> None:
            provider = provider_field.value or ''
            url = (url_field.value or '').strip()
            self._capture_provider = provider
            self._capture_url = url
            if not provider or not url:
                self._error('Choose a provider and enter a chat URL.')
                return
            self.page.pop_dialog()
            await self._run([provider], 'capture', url=url)
            if self._captured_path is not None:
                await self._diagnostics()

        controls: list[ft.Control] = [
            ft.Divider(),
            ft.Text('Capture a chat', weight=ft.FontWeight.BOLD),
            ft.Text('Record one chat’s provider responses with the chat’s text replaced, '
                    'to attach to a bug report. Open the file and check it before sharing.'),
            ft.Row([provider_field, url_field,
                    ft.Button('Capture', on_click=capture_chat, key='diagnostics-capture',
                              disabled=self._running or self.blocked)]),
        ]
        if self._captured_path is not None:
            folder = self._captured_path.parent
            controls.append(ft.Row([
                ft.Text(f'Saved capture: {self._captured_path}',
                        selectable=True, expand=True, key='capture-saved'),
                ft.Button('Open folder', on_click=lambda _: self._open(folder),
                          key='capture-open-folder'),
            ]))
        return controls

    async def _diagnostics(self, _=None) -> None:
        try:
            checks = await asyncio.to_thread(doctor.run_checks, self.root)
        except Exception as exc:
            self._error(f'Could not run the diagnostics: {exc}')
            return
        colors = {doctor.OK: ft.Colors.PRIMARY, doctor.PROBLEM: ft.Colors.ERROR,
                  doctor.WARNING: ft.Colors.TERTIARY, doctor.INFO: ft.Colors.ON_SURFACE_VARIANT}
        rows: list[ft.Control] = []
        for check in [*checks, *self._live_results]:
            area = DISPLAY_NAMES.get(check.area, check.area)
            lines: list[ft.Control] = [ft.Text(f'{area}: {check.detail}', selectable=True)]
            if check.advice:
                lines.append(ft.Text(check.advice, italic=True, selectable=True))
            rows.append(ft.Row([
                ft.Text(check.status.upper(), width=78, weight=ft.FontWeight.BOLD,
                        color=colors[check.status]),
                ft.Column(lines, spacing=2, expand=True)],
                vertical_alignment=ft.CrossAxisAlignment.START))

        async def live(_=None) -> None:
            self.page.pop_dialog()
            await self._run(list(self.enabled), 'check')
            if self._live_results:
                await self._diagnostics()

        async def copy(_=None) -> None:
            try:
                await ft.Clipboard().set(doctor.report(checks, self._live_results))
            except Exception as exc:
                self._error(f'Could not copy to the clipboard: {exc}')
                return
            self.summary.value = 'Diagnostics copied. They contain folder paths but no chats.'
            self.page.update()

        self.page.show_dialog(ft.AlertDialog(
            modal=True,
            title='Diagnostics',
            content=ft.Container(width=660, content=ft.Column(
                [ft.Text('Run live check logs in to each enabled provider and lists its '
                         'chats; it saves nothing.'), *rows, *self._capture_controls()],
                spacing=10, scroll=ft.ScrollMode.AUTO, height=440)),
            actions=[ft.Button('Run live check', on_click=live, key='diagnostics-live',
                               disabled=self._running or self.blocked or not self.enabled),
                     ft.Button('Copy diagnostics', on_click=copy, key='diagnostics-copy'),
                     ft.Button('Open log folder', on_click=self._open_logs),
                     ft.FilledButton('Close', on_click=lambda _: self.page.pop_dialog(),
                                     key='diagnostics-close')]))

    def _open(self, path: Path) -> None:
        try:
            open_directory(path)
        except Exception as exc:
            self._error(str(exc))

    def _open_archive(self, _=None) -> None:
        path = self._database_path()
        if path is not None:
            self._open(path)

    def _open_logs(self, _=None) -> None:
        folder = logs.log_dir(self.root)
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self._error(f'Could not open the log folder: {exc}')
            return
        self._open(folder)

    @staticmethod
    def _retire_locked(archive: Path) -> list[tidy.DuplicateCopy]:
        with locks.hold(archive, 'update'):
            return tidy.retire_duplicates(archive)

    async def _tidy_dialog(self, _=None) -> None:
        archive = self._database_path()
        if archive is None:
            return
        state = await asyncio.to_thread(database.inspect, archive)
        if not state.usable:
            self._error(state.message())
            return
        copies = await asyncio.to_thread(tidy.find_duplicates, archive)
        rows: list[ft.Control] = [
            ft.Text(
                f'{DISPLAY_NAMES.get(c.provider, c.provider)}: {c.title}'
                + (' (kept: contains unique branch text)' if c.has_unique_turns
                   else f' ({c.stale_dir.name} -> {c.keep_dir.name})'),
                selectable=True,
            )
            for c in copies
        ] or [ft.Text('No duplicate chat folders found.')]
        safe_count = sum(1 for c in copies if not c.has_unique_turns)

        async def confirm_tidy(_=None) -> None:
            self.page.pop_dialog()
            try:
                retired = await asyncio.to_thread(self._retire_locked, archive)
            except locks.LockedError as exc:
                self._lock_dialog(exc)
                return
            except OSError as exc:
                self._error(f'Could not tidy duplicates: {exc}')
                return
            self.summary.value = f'Retired {len(retired)} stale duplicate folder(s).'
            await self.refresh_archive()

        self.page.show_dialog(ft.AlertDialog(
            modal=True,
            key='tidy-dialog',
            title='Tidy duplicates',
            content=ft.Container(
                width=580,
                content=ft.Column(rows, spacing=8, scroll=ft.ScrollMode.AUTO, height=340),
            ),
            actions=[
                ft.OutlinedButton('Close', on_click=lambda _: self.page.pop_dialog()),
                ft.FilledButton('Retire duplicates', on_click=confirm_tidy,
                                key='confirm-tidy', disabled=safe_count == 0),
            ],
        ))

    @staticmethod
    def _restore_locked(archive: Path, selected: list[lifecycle.StateChatEntry]) -> int:
        with locks.hold(archive, 'update'):
            return sum(1 for entry in selected
                       if lifecycle.restore_state_chat(archive, entry) is not None)

    @staticmethod
    def _delete_locked(archive: Path, selected: list[lifecycle.StateChatEntry]) -> int:
        with locks.hold(archive, 'update'):
            return sum(1 for entry in selected
                       if lifecycle.delete_state_chat_permanently(archive, entry))

    async def _archived_deleted_dialog(self, _=None) -> None:
        archive = self._database_path()
        if archive is None:
            return
        state = await asyncio.to_thread(database.inspect, archive)
        if not state.usable:
            self._error(state.message())
            return
        entries = await asyncio.to_thread(lifecycle.list_state_chats, archive)
        items = [
            (entry, ft.Checkbox(
                label=f'[{DISPLAY_NAMES.get(entry.provider, entry.provider)} · {entry.state}] '
                      f'{entry.title}',
                value=False, key=f'state-chat-{entry.provider}-{entry.chat_id}'))
            for entry in entries
        ]
        row_controls: list[ft.Control] = [
            ft.Row([
                cb,
                ft.TextButton('Open', on_click=lambda _, p=entry.folder: self._open(p),
                              key=f'open-state-{entry.chat_id}'),
            ], alignment=ft.MainAxisAlignment.SPACE_BETWEEN)
            for entry, cb in items
        ] or [ft.Text('No archived or deleted chats in the database.')]

        async def restore(_=None) -> None:
            selected = [entry for entry, cb in items if cb.value]
            if not selected:
                return
            self.page.pop_dialog()
            try:
                count = await asyncio.to_thread(self._restore_locked, archive, selected)
            except locks.LockedError as exc:
                self._lock_dialog(exc)
                return
            self.summary.value = f'Restored {count} chat(s) back to active folders.'
            await self.refresh_archive()

        def ask_delete(_=None) -> None:
            selected = [entry for entry, cb in items if cb.value]
            if not selected:
                return
            self.page.pop_dialog()
            self._confirm_permanent_delete(archive, selected)

        self.page.show_dialog(ft.AlertDialog(
            modal=True,
            key='archived-deleted-dialog',
            title='Archived & deleted chats',
            content=ft.Container(
                width=600,
                content=ft.Column(row_controls, spacing=6,
                                  scroll=ft.ScrollMode.AUTO, height=340),
            ),
            actions=[
                ft.OutlinedButton('Close', on_click=lambda _: self.page.pop_dialog(),
                                  key='close-state-review'),
                ft.Button('Restore selected', on_click=restore,
                          key='restore-selected', disabled=not entries),
                ft.OutlinedButton('Delete permanently', on_click=ask_delete,
                                  key='delete-selected', disabled=not entries),
            ],
        ))

    def _confirm_permanent_delete(self, archive: Path,
                                  selected: list[lifecycle.StateChatEntry]) -> None:
        async def do_delete(_=None) -> None:
            self.page.pop_dialog()
            try:
                count = await asyncio.to_thread(self._delete_locked, archive, selected)
            except locks.LockedError as exc:
                self._lock_dialog(exc)
                return
            self.summary.value = f'Permanently deleted {count} chat folder(s).'
            await self.refresh_archive()

        self.page.show_dialog(ft.AlertDialog(
            modal=True,
            key='confirm-permanent-delete',
            title='Delete permanently?',
            content=ft.Text(
                f'This permanently deletes {len(selected)} chat folder(s) from '
                '_Archived/_Deleted on disk and cannot be undone.'),
            actions=[
                ft.OutlinedButton('Cancel', on_click=lambda _: self.page.pop_dialog()),
                ft.FilledButton('Delete permanently', on_click=do_delete,
                                key='confirm-delete-now'),
            ],
        ))

    def _settings(self, _=None) -> None:
        try:
            raw = config.load(self.root)
        except config.SettingsError as exc:
            self.settings_error = exc
            self._show_settings_problem(exc)
            return
        enabled_checks = {name: ft.Checkbox(label=DISPLAY_NAMES[name],
                                             value=name in self.enabled,
                                             key=f'settings-{name}')
                          for name in ALL_PROVIDERS}
        archive_field = ft.TextField(label='Database folder',
                                     value=str(self._database_path()
                                               or config.default_archive(self.root)),
                                     key='settings-archive', expand=True)
        profile_field = ft.TextField(
            label='Login data folder (optional)',
            value=str(raw.get('profile') or ''),
            hint_text=f'Default: {self.root / ".playwright-profile"}',
            key='settings-profile')
        kimi_field = ft.Dropdown(
            label='Kimi region',
            value=config.kimi_region(self.root),
            options=[
                ft.DropdownOption(key='international', text='International (kimi.ai)'),
                ft.DropdownOption(key='mainland', text='Mainland China (kimi.com)'),
            ],
            key='settings-kimi-region')
        chrome_field = ft.TextField(
            label='Chrome or Chromium location (optional)',
            value=str(raw.get('chrome_path') or ''),
            hint_text='Default: automatic detection',
            key='settings-chrome-path')
        timeout_field = ft.TextField(
            label='Login wait (seconds)',
            value=str(raw.get('login_timeout') or config.DEFAULT_LOGIN_TIMEOUT),
            key='settings-login-timeout')
        startup_check = ft.Checkbox(
            label='Check for updates when the app starts',
            value=config.check_on_startup(self.root),
            key='settings-check-on-startup')
        autostart_check = ft.Checkbox(
            label='Start Reclaim My Chats when I log in',
            value=config.start_at_login(self.root),
            key='settings-start-at-login')

        async def choose_archive(_=None) -> None:
            await self._pick_folder(archive_field, 'Choose database folder')

        async def choose_profile(_=None) -> None:
            await self._pick_folder(profile_field, 'Choose login data folder')

        async def save_settings(_=None) -> None:
            await self._save_settings(
                [name for name in ALL_PROVIDERS if enabled_checks[name].value],
                archive_field.value.strip(), profile_field.value.strip(),
                kimi_region=kimi_field.value or 'international',
                chrome_path=(chrome_field.value or '').strip(),
                login_timeout=(timeout_field.value or '').strip(),
                check_on_startup=bool(startup_check.value),
                start_at_login=bool(autostart_check.value))

        dialog = ft.AlertDialog(
            modal=True,
            title='Settings',
            content=ft.Container(
                width=570,
                content=ft.Column([
                    ft.Text('Enabled providers'),
                    *(ft.Row([check]) for check in enabled_checks.values()),
                    ft.Row([archive_field,
                            ft.Button('Browse', on_click=choose_archive,
                                      key='browse-archive')]),
                    ft.Row([profile_field,
                            ft.Button('Browse', on_click=choose_profile,
                                      key='browse-profile')]),
                    ft.Text('The login data folder stores browser login sessions. Leave it blank to use this app’s folder.'),
                    kimi_field,
                    chrome_field,
                    timeout_field,
                    startup_check,
                    autostart_check,
                ], scroll=ft.ScrollMode.AUTO, height=430),
            ),
            actions=[ft.OutlinedButton('Cancel', on_click=lambda _: self.page.pop_dialog()),
                     ft.FilledButton('Save', on_click=save_settings,
                                     key='save-settings')],
        )
        self.page.show_dialog(dialog)

    async def _pick_folder(self, field, title: str) -> None:
        """Fill `field` with a folder chosen in the system dialog."""
        try:
            picked = await ft.FilePicker().get_directory_path(dialog_title=title)
        except Exception as exc:
            self._error(f'Directory picker unavailable: {exc}. Enter the path above.')
            return
        if picked:
            field.value = picked
            self.page.update()

    async def _save_settings(self, enabled: list[str], archive: str, profile: str, *,
                             kimi_region: str = 'international', chrome_path: str = '',
                             login_timeout: str | int = config.DEFAULT_LOGIN_TIMEOUT,
                             check_on_startup: bool | None = None,
                             start_at_login: bool | None = None) -> None:
        """Check the chosen database folder, then save. A new or empty folder
        becomes a database only after the user confirms."""
        if not enabled or not archive:
            self._error('Choose at least one provider and a database folder.')
            return
        try:
            timeout = int(str(login_timeout).strip() or config.DEFAULT_LOGIN_TIMEOUT)
        except ValueError:
            self._error('Login wait must be a whole number of at least 30 seconds.')
            return
        if timeout < 30:
            self._error('Login wait must be a whole number of at least 30 seconds.')
            return
        target = config.resolve_folder(self.root, archive)
        # The default location stays "default", so it may be created on first use.
        choice = None if target == config.default_archive(self.root) else archive
        state = await asyncio.to_thread(database.inspect, target)
        if state.kind in ('foreign', 'newer', 'file'):
            self._error(state.message())
            return

        async def commit() -> None:
            try:
                config.save(self.root, providers=enabled, archive=choice, profile=profile,
                            kimi_region=kimi_region, chrome_path=chrome_path,
                            login_timeout=timeout, check_on_startup=check_on_startup,
                            start_at_login=start_at_login)
                if start_at_login is not None:
                    with contextlib.suppress(OSError):
                        autostart.set_autostart(start_at_login)
            except (OSError, ValueError) as exc:
                self._error(str(exc))
                return
            self.settings_error = None
            self._reload_providers()
            await self.refresh_archive()

        self.page.pop_dialog()
        if state.kind in ('missing', 'empty') and choice is not None:
            self._confirm_create(target, then=commit)
            return
        await commit()

    def _setup_wizard(self, _=None) -> None:
        """First-run / reconfiguration setup: browser check, folders, providers, and logins."""
        try:
            raw = config.load(self.root)
        except config.SettingsError as exc:
            self.settings_error = exc
            self._show_settings_problem(exc)
            return
        chrome = browser.find_chrome(self.root)
        chrome_msg = (f'Chrome detected: {chrome}' if chrome
                      else 'No Chrome or Chromium found — install Chrome or enter its path below.')
        active = raw.get('providers') or self.enabled or ALL_PROVIDERS
        checks = {name: ft.Checkbox(label=DISPLAY_NAMES[name], value=name in active,
                                    key=f'setup-{name}')
                  for name in ALL_PROVIDERS}
        archive_field = ft.TextField(
            label='Database folder',
            value=str(self._database_path() or config.default_archive(self.root)),
            key='setup-archive', expand=True)
        profile_field = ft.TextField(
            label='Login data folder (optional)',
            value=str(raw.get('profile') or ''),
            hint_text=f'Default: {self.root / ".playwright-profile"}',
            key='setup-profile', expand=True)
        chrome_field = ft.TextField(
            label='Chrome or Chromium location (optional)',
            value=str(raw.get('chrome_path') or ''),
            hint_text='Default: automatic detection',
            key='setup-chrome-path')

        def selected() -> list[str]:
            return [name for name in ALL_PROVIDERS if checks[name].value]

        async def pick_archive(_=None) -> None:
            await self._pick_folder(archive_field, 'Choose database folder')

        async def pick_profile(_=None) -> None:
            await self._pick_folder(profile_field, 'Choose login data folder')

        async def save_only(_=None) -> None:
            await self._apply_setup(selected(), archive_field.value.strip(),
                                    profile_field.value.strip(), (chrome_field.value or '').strip(),
                                    login_after=False)

        async def save_and_login(_=None) -> None:
            await self._apply_setup(selected(), archive_field.value.strip(),
                                    profile_field.value.strip(), (chrome_field.value or '').strip(),
                                    login_after=True)

        self.page.show_dialog(ft.AlertDialog(
            modal=True,
            title='Setup Reclaim My Chats',
            content=ft.Container(
                width=580,
                content=ft.Column([
                    ft.Text(chrome_msg, key='setup-chrome-status'),
                    ft.Row([archive_field, ft.Button('Browse', on_click=pick_archive)]),
                    ft.Row([profile_field, ft.Button('Browse', on_click=pick_profile)]),
                    chrome_field,
                    ft.Text('Providers to synchronize'),
                    *(ft.Row([check]) for check in checks.values()),
                ], scroll=ft.ScrollMode.AUTO, height=430),
            ),
            actions=[
                ft.OutlinedButton('Cancel', on_click=lambda _: self.page.pop_dialog(),
                                  key='setup-cancel'),
                ft.Button('Save setup', on_click=save_only, key='setup-save'),
                ft.FilledButton('Save & log in', on_click=save_and_login,
                                key='setup-save-login'),
            ],
        ))

    async def _apply_setup(self, enabled: list[str], archive: str, profile: str,
                           chrome_path: str, *, login_after: bool) -> None:
        if not enabled or not archive:
            self._error('Choose at least one provider and a database folder.')
            return
        target = config.resolve_folder(self.root, archive)
        choice = None if target == config.default_archive(self.root) else archive
        state = await asyncio.to_thread(database.inspect, target)
        if state.kind in ('foreign', 'newer', 'file'):
            self._error(state.message())
            return
        self.page.pop_dialog()
        try:
            if state.kind in ('missing', 'empty'):
                await asyncio.to_thread(database.init, target)
            config.save(self.root, providers=enabled, archive=choice,
                        profile=profile, chrome_path=chrome_path)
        except (database.DatabaseError, OSError, ValueError) as exc:
            self._error(str(exc))
            return
        self.settings_error = None
        self._reload_providers()
        await self.refresh_archive()
        if login_after:
            await self._run(enabled, 'check')

    def _error(self, message: str) -> None:
        self.page.show_dialog(ft.AlertDialog(title='Error', content=ft.Text(message),
                                             actions=[ft.Button('Close',
                                                                on_click=lambda _: self.page.pop_dialog())]))


async def app(page) -> None:
    window = ReclaimWindow(page)
    await window.refresh_archive()
    await window.check_release_update()
    await window.run_startup_check_if_enabled()


def main() -> int:
    if ft is None:
        message = 'The GUI requires Flet. Install it with: pip install "reclaimmychats[gui]"'
        if sys.stderr is not None:
            print(message, file=sys.stderr)
        elif os.name == 'nt':
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, message, 'Reclaim My Chats', 0x10)
        return 2
    ft.run(app)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
