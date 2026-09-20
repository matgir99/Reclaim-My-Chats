"""Flet desktop frontend for native archive updates."""

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

from .core import config, status
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
        raise FileNotFoundError(f'Archive directory does not exist yet: {path}')
    if os.name == 'nt':
        os.startfile(str(path))
    elif sys.platform == 'darwin':
        subprocess.Popen(['open', str(path)])
    else:
        subprocess.Popen(['xdg-open', str(path)])


def status_text(info: status.ProviderStatus) -> str:
    if not info.archived:
        return 'No archive yet'
    summary = f'{info.chat_folders} chats archived'
    if info.last_sync:
        summary += f' · last sync {info.last_sync}'
    if info.last_run:
        summary += (f' · last run {info.last_run["ok"]} ok, '
                    f'{info.last_run["failed"]} failed')
    return summary


class ReclaimWindow:
    def __init__(self, page, root: Path | None = None,
                 controller: Controller | None = None):
        if ft is None:
            raise RuntimeError('Flet is required for the GUI')
        self.page = page
        self.root = Path(root) if root is not None else config.default_root()
        self.controller = controller or ArchiveController(self.root)
        self.enabled = configured_providers(self.root)
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

        self.archive_path = ft.Text(str(config.archive_root(self.root)),
                                    selectable=True, key='archive-path')
        self.archive_total = ft.Text('Loading archive status', key='archive-total')
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
        self.open_archive = ft.Button('Open archive', on_click=self._open_archive,
                                      key='open-archive')
        self.settings_button = ft.Button('Settings', on_click=self._settings,
                                         key='settings')

        page.add(ft.Container(
            padding=20,
            content=ft.Column([
                ft.Row([
                    ft.Text('Reclaim My Chats', size=28, weight=ft.FontWeight.BOLD),
                    self.settings_button,
                ], alignment=ft.MainAxisAlignment.SPACE_BETWEEN, wrap=True),
                ft.Text('Archive and synchronize your AI conversations'),
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
                ft.Text('Archive', size=20, weight=ft.FontWeight.BOLD),
                self.archive_total,
                self.archive_path,
                self.open_archive,
                ft.ExpansionTile(title='Details', controls=[self.log],
                                 key='details'),
            ], spacing=12),
        ))
        self._selection_changed()

    async def refresh_archive(self) -> None:
        archive = config.archive_root(self.root)
        self.archive_path.value = str(archive)
        try:
            infos = await asyncio.to_thread(status.scan, archive)
        except Exception as exc:
            self._error(f'Could not read archive status: {exc}')
            return
        self.archive_total.value = f'{sum(i.chat_folders for i in infos):,} chats archived'
        for info in infos:
            if info.provider in self.enabled:
                self.provider_status[info.provider].value = status_text(info)
            else:
                self.provider_status[info.provider].value = 'Disabled in settings'
        self.page.update()

    def _selected(self) -> list[str]:
        return [name for name in ALL_PROVIDERS
                if name in self.enabled and self.checks[name].value]

    def _selection_changed(self, _=None) -> None:
        selected = bool(self._selected())
        self.update_selected.disabled = self._running or not selected
        self.dry_run.disabled = self._running or not selected
        self.rebuild.disabled = self._running or not selected
        self.update_all.disabled = self._running or not self.enabled
        self.settings_button.disabled = self._running
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
        elif event.kind == 'run_completed':
            self.progress.value = 1
            self._append_log(f'Run completed: {event.message}')
        self.page.update()

    async def _run(self, providers: list[str], mode: str) -> None:
        if not providers:
            self._error('Select at least one enabled provider.')
            return
        if self._running or self.controller.busy:
            self._error('Another archive operation is already running.')
            return
        self._set_running(True)
        self.summary.value = ''
        self._log_lines.clear()
        self.log.value = ''
        self._provider_count = len(providers)
        queue: asyncio.Queue[ProgressEvent | None] = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def receive(event: ProgressEvent) -> None:
            loop.call_soon_threadsafe(queue.put_nowait, event)

        async def execute() -> RunSummary:
            try:
                return await asyncio.to_thread(self.controller.run, providers, mode, receive)
            finally:
                await queue.put(None)

        try:
            task = asyncio.create_task(execute())
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
            self.current.value = 'Ready'
        except BusyOperationError as exc:
            self._error(str(exc))
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
            title='Rebuild selected archives?',
            content=ft.Text(f'This fetches every conversation again and overwrites local copies for: {names}.'),
            actions=[ft.OutlinedButton('Cancel', on_click=lambda _: self.page.pop_dialog(),
                                       key='cancel-rebuild'),
                     ft.FilledButton('Rebuild', on_click=confirm, key='confirm-rebuild')],
        )
        self.page.show_dialog(dialog)

    def _open_archive(self, _=None) -> None:
        try:
            open_directory(config.archive_root(self.root))
        except Exception as exc:
            self._error(str(exc))

    def _settings(self, _=None) -> None:
        raw = config.load(self.root)
        enabled_checks = {name: ft.Checkbox(label=DISPLAY_NAMES[name],
                                             value=name in self.enabled,
                                             key=f'settings-{name}')
                          for name in ALL_PROVIDERS}
        archive_field = ft.TextField(label='Archive directory',
                                     value=str(config.archive_root(self.root)),
                                     key='settings-archive', expand=True)
        profile_field = ft.TextField(
            label='Existing browser profile (optional)',
            value=str(raw.get('profile') or ''),
            hint_text=f'Default: {self.root / ".playwright-profile"}',
            key='settings-profile')

        async def choose_archive(_=None) -> None:
            try:
                picked = await ft.FilePicker().get_directory_path(
                    dialog_title='Choose archive directory')
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
                self._error('Choose at least one provider and an archive directory.')
                return
            try:
                config.save(self.root, providers=enabled, archive=archive,
                            profile=profile_field.value.strip())
            except (OSError, ValueError) as exc:
                self._error(str(exc))
                return
            self.enabled = enabled
            for name in ALL_PROVIDERS:
                self.checks[name].disabled = name not in enabled
                self.checks[name].value = name in enabled
            self.page.pop_dialog()
            self._selection_changed()
            await self.refresh_archive()

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
