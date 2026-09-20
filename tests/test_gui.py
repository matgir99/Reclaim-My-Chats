"""Offline Flet control tests with a fake archive controller."""

from __future__ import annotations

import tempfile
import unittest
from io import StringIO
from pathlib import Path
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
        self.assertEqual(self.window.archive_total.value, '0 chats archived')

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


class TestOptionalGui(unittest.TestCase):
    def test_cli_entry_explains_missing_extra(self):
        stderr = StringIO()
        with patch.object(gui, 'ft', None), patch.object(gui.sys, 'stderr', stderr):
            self.assertEqual(gui.main(), 2)
        self.assertIn('reclaimmychats[gui]', stderr.getvalue())
