"""Flet desktop frontend for native database updates."""

from __future__ import annotations

import asyncio
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

from .core import config, database, locks, logs, status
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


class Controller(Protocol):
    @property
    def busy(self) -> bool: ...

    def run(self, providers: list[str], mode: str = 'update',
            sink: EventSink | None = None, *, log: bool = False) -> RunSummary: ...


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
        return 'No chats saved yet'
    summary = f'{info.chats} chats in database'
    if info.chat_folders > info.chats:
        summary += f' ({info.chat_folders - info.chats} older copies)'
    if info.last_sync:
        summary += f' · last sync {info.last_sync}'
    if info.last_run:
        summary += (f' · last run {info.last_run["ok"]} ok, '
                    f'{info.last_run["failed"]} failed')
    if info.last_error:
        summary += f' · last attempt failed: {info.last_error}'
    return summary


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
        self._running = False
        self._log_lines: list[str] = []
        self._provider_index = 0
        self._provider_count = 1
        self._build()

    def _build(self) -> None:
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
        rows = []
        for name in ALL_PROVIDERS:
            enabled = name in self.enabled
            check = ft.Checkbox(label=DISPLAY_NAMES[name], value=enabled,
                                disabled=not enabled, on_change=self._selection_changed,
                                key=f'provider-{name}')
            text = ft.Text('Loading' if enabled else 'Disabled in settings',
                           key=f'provider-status-{name}', expand=True)
            self.checks[name] = check
            self.provider_status[name] = text
            rows.append(ft.Row([check, text], alignment=ft.MainAxisAlignment.SPACE_BETWEEN))

        self.update_selected = ft.FilledButton('Update selected',
                                               on_click=self._update_selected,
                                               key='update-selected')
        self.update_all = ft.Button('Update all', on_click=self._update_all,
                                    key='update-all')
        self.dry_run = ft.Button('Dry run', on_click=self._dry_run,
                                 key='dry-run')
        self.rebuild = ft.OutlinedButton('Rebuild selected',
                                         on_click=self._confirm_rebuild,
                                         key='rebuild')
        self.open_archive = ft.Button('Open database folder', on_click=self._open_archive,
                                      key='open-archive')
        self.open_logs = ft.Button('Open log folder', on_click=self._open_logs,
                                   key='open-logs')
        self.notice_text = ft.Text('', selectable=True, key='notice-text',
                                   color=ft.Colors.ON_ERROR_CONTAINER)
        self.notice_actions = ft.Row([], wrap=True, key='notice-actions')
        self.notice = ft.Container(
            content=ft.Column([self.notice_text, self.notice_actions], spacing=10),
            bgcolor=ft.Colors.ERROR_CONTAINER, padding=14, border_radius=8,
            visible=False, key='notice')
        self.settings_button = ft.Button('Settings', on_click=self._settings,
                                         key='settings')

        page.add(ft.Container(
            padding=20,
            content=ft.Column([
                ft.Row([
                    ft.Text('Reclaim My Chats', size=28, weight=ft.FontWeight.BOLD),
                    self.settings_button,
                ], alignment=ft.MainAxisAlignment.SPACE_BETWEEN, wrap=True),
                ft.Text('Save and synchronize your AI conversations in a local database'),
                self.notice,
                ft.Divider(),
                ft.Text('Providers', size=20, weight=ft.FontWeight.BOLD),
                *rows,
                ft.Row([self.update_selected, self.update_all], wrap=True),
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
                ft.Row([self.open_archive, self.open_logs], wrap=True),
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

    async def refresh_archive(self) -> None:
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
            infos = await asyncio.to_thread(status.scan, archive)
        except Exception as exc:
            self._error(f'Could not read database status: {exc}')
            return
        first_use = state.kind in ('missing', 'empty') and config.uses_default_database(self.root)
        if state.usable or first_use:
            self._hide_notice()
        else:
            self._show_database_problem(state)
        self.archive_total.value = f'{sum(i.chats for i in infos):,} chats in the database'
        for info in infos:
            if info.provider in self.enabled:
                self.provider_status[info.provider].value = status_text(info)
            else:
                self.provider_status[info.provider].value = 'Disabled in settings'
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
            self.checks[name].disabled = name not in self.enabled
            self.checks[name].value = name in self.enabled
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
        # Settings cannot be edited while the settings file itself is broken.
        self.settings_button.disabled = self._running or self.settings_error is not None
        self.page.update()

    def _set_running(self, running: bool) -> None:
        self._running = running
        self._selection_changed()

    def _append_log(self, line: str) -> None:
        self._log_lines.append(line)
        self._log_lines = self._log_lines[-200:]
        self.log.value = '\n'.join(self._log_lines)

    def _event(self, event: ProgressEvent) -> None:
        name = DISPLAY_NAMES.get(event.provider, event.provider)
        if event.kind == 'run_started':
            self.current.value = f'Starting {event.message}'
            self.progress.value = 0
            self._append_log(f'Run started: {event.message}')
        elif event.kind == 'provider_started':
            self._provider_index = event.current
            self._provider_count = event.total
            self.current.value = f'{name}: listing conversations or waiting for login'
            self.provider_status[event.provider].value = 'Running'
            self._append_log(f'{name}: started')
        elif event.kind in ('chat_started', 'chat_skipped', 'chat_completed',
                            'chat_planned', 'error'):
            if event.title:
                self.current.value = f'{name}: {event.title}'
            if event.total:
                completed = event.current - (1 if event.kind == 'chat_started' else 0)
                self.progress.value = min(1, ((self._provider_index - 1) +
                                          completed / event.total) / self._provider_count)
            detail = event.message or event.kind.replace('_', ' ')
            self._append_log(f'{name}: {event.title} — {detail}')
            if event.kind == 'error':
                self.provider_status[event.provider].value = f'Error: {event.message}'
        elif event.kind == 'provider_completed':
            self.progress.value = event.current / event.total
            if event.message == 'exit 0':
                self.provider_status[event.provider].value = 'Completed'
            elif not str(self.provider_status[event.provider].value).startswith('Error:'):
                self.provider_status[event.provider].value = f'Failed ({event.message})'
            self._append_log(f'{name}: {event.message}')
        elif event.kind == 'warning':
            self._warnings += 1
            self._append_log(f'{name}: warning: {event.title} — {event.message}')
        elif event.kind == 'run_completed':
            self.progress.value = 1
            self._append_log(f'Run completed: {event.message}')
        self.page.update()

    async def _run(self, providers: list[str], mode: str) -> None:
        if not providers:
            self._error('Select at least one enabled provider.')
            return
        if self._running or self.controller.busy:
            self._error('Another operation is already running.')
            return
        if self.blocked:
            self._error('Fix the problem shown at the top of the window first.')
            return
        self._set_running(True)
        self.summary.value = ''
        self._warnings = 0
        self._log_lines.clear()
        self.log.value = ''
        self._provider_count = len(providers)
        queue: asyncio.Queue[ProgressEvent | None] = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def receive(event: ProgressEvent) -> None:
            loop.call_soon_threadsafe(queue.put_nowait, event)

        def execute() -> RunSummary:
            try:
                return self.controller.run(providers, mode, receive)
            finally:
                # Use the same thread-safe channel as events so completion
                # cannot overtake progress/error messages still in transit.
                loop.call_soon_threadsafe(queue.put_nowait, None)

        try:
            task = asyncio.create_task(asyncio.to_thread(execute))
            while (event := await queue.get()) is not None:
                self._event(event)
            result = await task
            labels = ', '.join(f'{DISPLAY_NAMES[name]}: {"ok" if code == 0 else "failed"}'
                               for name, code in result.codes)
            action = {'update': 'Update', 'rebuild': 'Rebuild',
                      'dry-run': 'Dry run'}[mode]
            state = ('completed successfully' if result.exit_code == 0
                     else 'completed with errors')
            self.summary.value = f'{action} {state} · {labels}'
            if self._warnings:
                self.summary.value += (f' · {self._warnings} warning'
                                       f'{"s" if self._warnings != 1 else ""} (see Details)')
            self.current.value = 'Ready'
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
            if mode != 'dry-run':
                await self.refresh_archive()
            self.page.update()

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
            label='Existing browser profile (optional)',
            value=str(raw.get('profile') or ''),
            hint_text=f'Default: {self.root / ".playwright-profile"}',
            key='settings-profile')

        async def choose_archive(_=None) -> None:
            try:
                picked = await ft.FilePicker().get_directory_path(
                    dialog_title='Choose database folder')
                if picked:
                    archive_field.value = picked
                    self.page.update()
            except Exception as exc:
                self._error(f'Directory picker unavailable: {exc}. Enter the path above.')

        async def choose_profile(_=None) -> None:
            try:
                picked = await ft.FilePicker().get_directory_path(
                    dialog_title='Choose existing Playwright profile')
                if picked:
                    profile_field.value = picked
                    self.page.update()
            except Exception as exc:
                self._error(f'Directory picker unavailable: {exc}. Enter the path above.')

        async def save_settings(_=None) -> None:
            enabled = [name for name in ALL_PROVIDERS if enabled_checks[name].value]
            archive = archive_field.value.strip()
            if not enabled or not archive:
                self._error('Choose at least one provider and a database folder.')
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
                    config.save(self.root, providers=enabled, archive=choice,
                                profile=profile_field.value.strip())
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
                    ft.Text('The profile stores browser login sessions. Leave it blank to use this app’s profile.'),
                ], scroll=ft.ScrollMode.AUTO, height=430),
            ),
            actions=[ft.OutlinedButton('Cancel', on_click=lambda _: self.page.pop_dialog()),
                     ft.FilledButton('Save', on_click=save_settings,
                                     key='save-settings')],
        )
        self.page.show_dialog(dialog)

    def _error(self, message: str) -> None:
        self.page.show_dialog(ft.AlertDialog(title='Error', content=ft.Text(message),
                                             actions=[ft.Button('Close',
                                                                on_click=lambda _: self.page.pop_dialog())]))


async def app(page) -> None:
    window = ReclaimWindow(page)
    await window.refresh_archive()


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
