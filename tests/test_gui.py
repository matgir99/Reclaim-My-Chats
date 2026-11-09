"""Offline Flet control tests with a fake archive controller."""

from __future__ import annotations

import tempfile
import unittest
from io import StringIO
from pathlib import Path
from typing import Any
from unittest.mock import patch

from reclaim.core.events import ProgressEvent
from reclaim.core.service import RunSummary
from reclaim import gui


class _Window:
    pass


class _Page:
    def __init__(self):
        self.window = _Window()
        self.controls = []
        self.dialog = None

    def add(self, *controls):
        self.controls.extend(controls)

    def update(self, *_):
        pass

    def show_dialog(self, dialog):
        self.dialog = dialog

    def pop_dialog(self):
        previous = self.dialog
        self.dialog = None
        return previous


class _Controller:
    busy = False

    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def run(self, providers, mode='update', sink=None, *, log=False):
        self.calls.append((list(providers), mode))
        assert sink is not None
        sink(ProgressEvent('run_started', total=len(providers), message=mode))
        sink(ProgressEvent('provider_started', providers[0], current=1,
                           total=len(providers)))
        sink(ProgressEvent('chat_started', providers[0], 'Example', 1, 1))
        sink(ProgressEvent('error' if self.fail else 'chat_completed',
                           providers[0], 'Example', 1, 1,
                           'Synthetic failure' if self.fail else ''))
        sink(ProgressEvent('provider_completed', providers[0], current=1,
                           total=len(providers),
                           message='exit 2' if self.fail else 'exit 0'))
        sink(ProgressEvent('run_completed', current=len(providers),
                           total=len(providers),
                           message='exit 2' if self.fail else 'exit 0'))
        return RunSummary(((providers[0], 2 if self.fail else 0),))


@unittest.skipUnless(gui.ft is not None, 'Flet GUI extra is not installed')
class TestGui(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.page = _Page()
        self.controller = _Controller()
        self.window = gui.ReclaimWindow(self.page, self.root, self.controller)
        await self.window.refresh_archive()

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def test_opens_with_all_providers_and_archive_path(self):
        self.assertEqual(len(self.window.checks), 6)
        self.assertEqual(self.window.archive_path.value, str(self.root / 'chats'))
        self.assertEqual(self.window.archive_total.value, '0 chats in the database')

    async def test_selected_update_and_dry_run_use_selection(self):
        self.window.checks['kimi'].value = False
        await self.window._update_selected()
        self.assertEqual(self.controller.calls[0],
                         ([p for p in gui.ALL_PROVIDERS if p != 'kimi'], 'update'))
        await self.window._dry_run()
        self.assertEqual(self.controller.calls[1][1], 'dry-run')
        self.assertEqual(self.window.progress.value, 1)
        self.assertIn('completed successfully', self.window.summary.value)

    async def test_update_all_ignores_temporary_selection(self):
        self.window.checks['kimi'].value = False
        await self.window._update_all()
        self.assertEqual(self.controller.calls[0], (gui.ALL_PROVIDERS, 'update'))

    async def test_rebuild_requires_confirmation(self):
        self.window._confirm_rebuild()
        self.assertEqual(self.controller.calls, [])
        assert self.page.dialog is not None
        assert self.page.dialog.actions is not None
        confirm = self.page.dialog.actions[1]
        await confirm.on_click(None)
        self.assertEqual(self.controller.calls[0][1], 'rebuild')

    async def test_settings_persist(self):
        self.window._settings()
        dialog = self.page.dialog
        assert dialog is not None
        assert dialog.actions is not None
        dialog.content.content.controls[1].controls[0].value = False  # Google AI Studio
        await dialog.actions[1].on_click(None)
        self.assertNotIn('googleaistudio', gui.config.load(self.root)['providers'])
        self.assertTrue(self.window.checks['googleaistudio'].disabled)

    async def test_failure_is_visible(self):
        self.controller.fail = True
        await self.window._update_selected()
        self.assertIn('completed with errors', self.window.summary.value)
        self.assertIn('Synthetic failure', self.window.log.value)


class _RaisingController(_Controller):
    def __init__(self, exc):
        super().__init__()
        self.exc = exc

    def run(self, providers, mode='update', sink=None, *, log=False):
        self.calls.append((list(providers), mode))
        raise self.exc


class _WarningController(_Controller):
    def run(self, providers, mode='update', sink=None, *, log=False):
        assert sink is not None
        sink(ProgressEvent('warning', providers[0], 'Example', message='raw.json was not saved'))
        return super().run(providers, mode, sink, log=log)


@unittest.skipUnless(gui.ft is not None, 'Flet GUI extra is not installed')
class TestGuiSafeguards(unittest.IsolatedAsyncioTestCase):
    """Phase 1 problems are explained in the window, with a way to fix them."""

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.page = _Page()

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def open(self, controller=None):
        window = gui.ReclaimWindow(self.page, self.root, controller or _Controller())
        await window.refresh_archive()
        return window

    def actions(self, window) -> dict[str, Any]:
        return {str(control.key): control for control in window.notice_actions.controls}

    async def test_invalid_settings_show_a_banner_and_can_be_reset(self):
        (self.root / '.reclaim.json').write_text('{"providers": ["chatgpt"],}')
        window = await self.open()
        self.assertTrue(window.notice.visible)
        self.assertIn('is not valid JSON', window.notice_text.value)
        self.assertTrue(window.update_all.disabled)
        self.assertTrue(window.settings_button.disabled)
        await self.actions(window)['notice-reset-settings'].on_click(None)
        self.assertFalse(window.notice.visible)
        self.assertFalse(window.update_all.disabled)
        self.assertEqual(len(window.enabled), 6)
        self.assertIn('old file was kept', window.summary.value)
        self.assertEqual(len(list(self.root.glob('.reclaim.json.invalid-*'))), 1)

    async def test_missing_database_offers_retry_choose_and_create(self):
        target = self.root / 'unmounted' / 'chats'
        gui.config.save(self.root, providers=['chatgpt'], archive=str(target))
        controller = _Controller()
        window = await self.open(controller)
        self.assertTrue(window.notice.visible)
        self.assertIn('was not found', window.notice_text.value)
        self.assertEqual(sorted(self.actions(window)), ['notice-choose-folder',
                                                        'notice-create-database', 'notice-retry'])
        await window._update_all()
        self.assertEqual(controller.calls, [])  # blocked: no run starts
        self.actions(window)['notice-create-database'].on_click(None)
        assert self.page.dialog is not None and self.page.dialog.actions is not None
        await self.page.dialog.actions[1].on_click(None)
        self.assertTrue((target / gui.database.MARKER).exists())
        self.assertFalse(window.notice.visible)

    async def test_default_database_on_first_use_needs_no_banner(self):
        window = await self.open()
        self.assertFalse(window.notice.visible)
        self.assertFalse(window.update_all.disabled)

    async def test_locked_database_shows_who_holds_it_and_can_be_unlocked(self):
        folder = self.root / 'chats'
        gui.database.init(folder)
        (folder / gui.locks.LOCK_NAME).write_text('{"host": "laptop-elsewhere", "pid": 1}')
        holder = gui.locks.read_holder(folder) or {}
        window = await self.open(_RaisingController(
            gui.locks.LockedError(folder, holder, removable=True)))
        await window._update_all()
        dialog = self.page.dialog
        assert dialog is not None and dialog.actions is not None
        self.assertIn('laptop-elsewhere', dialog.content.value)
        dialog.actions[1].on_click(None)  # Remove lock
        self.assertIsNone(gui.locks.read_holder(folder))
        self.assertIn('Lock removed', window.summary.value)

    async def test_warnings_appear_in_the_summary(self):
        window = await self.open(_WarningController())
        await window._update_all()
        self.assertIn('1 warning (see Details)', window.summary.value)
        self.assertIn('raw.json was not saved', window.log.value)

    async def test_choosing_an_empty_folder_creates_a_database_after_confirmation(self):
        window = await self.open()
        new_folder = self.root / 'synced' / 'Chats'
        window._settings()
        dialog = self.page.dialog
        assert dialog is not None and dialog.actions is not None
        archive_row = dialog.content.content.controls[7]
        archive_row.controls[0].value = str(new_folder)
        await dialog.actions[1].on_click(None)  # Save
        confirm = self.page.dialog
        assert confirm is not None and confirm.actions is not None
        self.assertEqual(confirm.title, 'Create a new database?')
        await confirm.actions[1].on_click(None)
        self.assertTrue((new_folder / gui.database.MARKER).exists())
        self.assertEqual(gui.config.load(self.root)['archive'], str(new_folder))
        self.assertEqual(window.archive_path.value, str(new_folder))


class _CheckController(_Controller):
    def run(self, providers, mode='update', sink=None, *, log=False):
        self.calls.append((list(providers), mode))
        assert sink is not None
        for name in providers:
            sink(ProgressEvent('checked', name, message='logged in · 3 chats listed'))
        return RunSummary(tuple((name, 0) for name in providers))


class _Clipboard:
    copied: list[str] = []

    async def set(self, value):
        _Clipboard.copied.append(value)


@unittest.skipUnless(gui.ft is not None, 'Flet GUI extra is not installed')
class TestGuiDiagnostics(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.page = _Page()
        self.controller = _CheckController()
        self.window = gui.ReclaimWindow(self.page, self.root, self.controller)
        await self.window.refresh_archive()

    async def asyncTearDown(self):
        self.temp.cleanup()

    def dialog_text(self):
        dialog = self.page.dialog
        assert dialog is not None
        texts = []
        for row in dialog.content.content.controls[1:]:
            texts.extend(c.value for c in row.controls[1].controls)
        return ' '.join(texts)

    async def test_panel_shows_checks_and_runs_the_live_check(self):
        await self.window._diagnostics()
        self.assertIn('Settings: defaults (no settings file)', self.dialog_text())
        actions = {a.key: a for a in self.page.dialog.actions}
        await actions['diagnostics-live'].on_click(None)
        self.assertEqual(self.controller.calls[0][1], 'check')
        self.assertIn('Live check completed successfully', self.window.summary.value)
        self.assertIn('ChatGPT: logged in · 3 chats listed', self.dialog_text())
        self.assertEqual(self.window.provider_status['kimi'].value,
                         'Live check: logged in · 3 chats listed')

    async def test_copy_diagnostics(self):
        await self.window._diagnostics()
        actions = {a.key: a for a in self.page.dialog.actions}
        with patch.object(gui.ft, 'Clipboard', _Clipboard):
            await actions['diagnostics-copy'].on_click(None)
        self.assertIn('Reclaim My Chats diagnostics', _Clipboard.copied[-1])
        self.assertIn('Diagnostics copied', self.window.summary.value)


class TestOptionalGui(unittest.TestCase):
    def test_cli_entry_explains_missing_extra(self):
        stderr = StringIO()
        with patch.object(gui, 'ft', None), patch.object(gui.sys, 'stderr', stderr):
            self.assertEqual(gui.main(), 2)
        self.assertIn('reclaimmychats[gui]', stderr.getvalue())
