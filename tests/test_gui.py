"""Offline Flet control tests with a fake archive controller."""

from __future__ import annotations

import tempfile
import unittest
from io import StringIO
from pathlib import Path
from typing import Any, ClassVar
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
        self.capture_calls = []
        self.only_ids = None
        self.stopped = False
        self.brought_forward = False
        self.fail = fail

    def run(self, providers, mode='update', sink=None, *, log=False, only_ids=None):
        self.calls.append((list(providers), mode))
        if only_ids is not None:
            self.only_ids = list(only_ids)
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

    def capture(self, provider, url, sink=None, destination=None):
        self.capture_calls.append((provider, url, destination))
        assert sink is not None
        path = Path(destination or (Path(tempfile.gettempdir()) / f'{provider}-capture.json'))
        sink(ProgressEvent('run_started', total=1, message='capture'))
        sink(ProgressEvent('provider_started', provider, current=1, total=1))
        sink(ProgressEvent('captured', provider, message='1 response recorded', detail=str(path)))
        sink(ProgressEvent('provider_completed', provider, current=1, total=1, message='exit 0'))
        sink(ProgressEvent('run_completed', current=1, total=1, message='exit 0'))
        return RunSummary(((provider, 0),))

    def stop(self) -> None:
        self.stopped = True

    def bring_browser_forward(self) -> bool:
        self.brought_forward = True
        return True


@unittest.skipUnless(gui.ft is not None, 'Flet GUI extra is not installed')
class TestGui(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.page = _Page()
        self.controller = _Controller()
        self.notify_patch = patch.object(gui.autostart, 'notify_desktop', return_value=True)
        self.notify_mock = self.notify_patch.start()
        self.window = gui.ReclaimWindow(self.page, self.root, self.controller)
        await self.window.refresh_archive()

    async def asyncTearDown(self):
        self.notify_patch.stop()
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
        dialog.content.content.controls[10].value = 'mainland'         # Kimi region
        dialog.content.content.controls[11].value = '/usr/bin/chrome'  # Chrome location
        dialog.content.content.controls[12].value = '180'              # Login wait
        dialog.content.content.controls[13].value = True               # Check on startup
        dialog.content.content.controls[14].value = True               # Start at login
        with patch.object(gui.autostart, 'set_autostart') as set_auto:
            await dialog.actions[1].on_click(None)
        set_auto.assert_called_once_with(True)
        saved = gui.config.load(self.root)
        self.assertNotIn('googleaistudio', saved['providers'])
        self.assertEqual(saved['kimi_region'], 'mainland')
        self.assertEqual(saved['chrome_path'], '/usr/bin/chrome')
        self.assertEqual(saved['login_timeout'], 180)
        self.assertTrue(saved['check_on_startup'])
        self.assertTrue(saved['start_at_login'])
        self.assertTrue(self.window.checks['googleaistudio'].disabled)

    async def test_review_dialog_merges_subset_and_startup_check(self):
        class _PlanController(_Controller):
            def run(self, providers, mode='update', sink=None, *, log=False, only_ids=None):
                self.calls.append((list(providers), mode))
                if only_ids is not None:
                    self.only_ids = list(only_ids)
                assert sink is not None
                if mode == 'dry-run':
                    sink(ProgressEvent('chat_planned', 'chatgpt', 'Chat One', 1, 2,
                                       'fetch (new)', detail='id-1'))
                    sink(ProgressEvent('chat_planned', 'chatgpt', 'Chat Two', 2, 2,
                                       'fetch (changed)', detail='id-2'))
                return RunSummary(((providers[0], 0),))

        ctrl = _PlanController()
        self.window.controller = ctrl
        for k, v in self.window.checks.items():
            v.value = k == 'chatgpt'
        await self.window._dry_run()
        dialog = self.page.dialog
        assert dialog is not None and dialog.actions is not None
        self.assertEqual(dialog.title, 'Review updates')
        dialog.content.content.controls[1].value = False  # uncheck Chat Two
        await dialog.actions[1].on_click(None)  # Merge selected
        self.assertEqual(ctrl.calls[-1], (['chatgpt'], 'update'))
        self.assertEqual(ctrl.only_ids, ['id-1'])

        gui.config.save(self.root, providers=['chatgpt'], archive=None, check_on_startup=True)
        ctrl.calls.clear()
        self.page.pop_dialog()
        await self.window.run_startup_check_if_enabled()
        self.assertEqual(ctrl.calls[-1], (['googleaistudio', 'deepseek', 'kimi', 'chatgpt',
                                           'claude', 'googlegemini'], 'dry-run'))

    async def test_review_dialog_shows_moves_pairs_restore_and_confirms_brake(self):
        class _MoveController(_Controller):
            def run(self, providers, mode='update', sink=None, *, log=False, only_ids=None):
                self.calls.append((list(providers), mode))
                if only_ids is not None:
                    self.only_ids = list(only_ids)
                assert sink is not None
                if mode == 'dry-run':
                    sink(ProgressEvent('chat_planned', 'chatgpt', 'Gone Chat', 1, 2,
                                       'move (deleted)', detail='id-gone'))
                    sink(ProgressEvent('chat_planned', 'chatgpt', 'Restored Chat', 2, 2,
                                       'move (active)', detail='id-rest'))
                    sink(ProgressEvent('chat_planned', 'chatgpt', 'Restored Chat', 1, 1,
                                       'fetch (restored)', detail='id-rest'))
                    sink(ProgressEvent('warning', 'chatgpt',
                                       message='Mass-change brake: 1 of 5 chats would move to archived/deleted; review required.'))
                return RunSummary(((providers[0], 0),))

        ctrl = _MoveController()
        self.window.controller = ctrl
        for k, v in self.window.checks.items():
            v.value = k == 'chatgpt'
        await self.window._dry_run()
        dialog = self.page.dialog
        assert dialog is not None and dialog.actions is not None
        controls = dialog.content.content.controls
        self.assertEqual(controls[0].key, 'review-brake-0')
        self.assertIn('Mass-change brake', controls[0].value)
        # Three planned events collapse to two checkboxes (Gone Chat + combined Restored Chat).
        self.assertEqual(len(controls), 3)
        self.assertIn('move (deleted)', controls[1].label)
        self.assertIn('move (active) + fetch (restored)', controls[2].label)
        await dialog.actions[2].on_click(None)  # Merge all passes confirmed IDs past brake
        self.assertEqual(ctrl.only_ids, ['id-gone', 'id-rest'])

    async def test_startup_check_disables_login_wait_in_worker_and_keeps_needs_login(self):
        observed_wait_disabled: list[bool] = []

        class _StartupController(_Controller):
            def run(self, providers, mode='update', sink=None, *, log=False, only_ids=None):
                observed_wait_disabled.append(gui.browser.login_wait_disabled())
                assert sink is not None
                sink(ProgressEvent('run_started', total=len(providers), message=mode))
                sink(ProgressEvent('provider_started', 'chatgpt', current=1, total=len(providers)))
                sink(ProgressEvent('login_needed', 'chatgpt', message='ChatGPT requires a login.'))
                sink(ProgressEvent('provider_completed', 'chatgpt', current=1,
                                   total=len(providers), message='exit 2'))
                sink(ProgressEvent('run_completed', current=1, total=len(providers),
                                   message='exit 2'))
                return RunSummary((('chatgpt', 2),))

        gui.config.save(self.root, providers=['chatgpt'], archive=None, check_on_startup=True)
        self.window._reload_providers()
        self.window.controller = _StartupController()
        await self.window.run_startup_check_if_enabled()
        self.assertEqual(observed_wait_disabled, [True])
        self.assertEqual(self.window.provider_chips['chatgpt'].value, 'Needs login')
        self.assertEqual(self.window.provider_status['chatgpt'].value,
                         'Needs login: ChatGPT requires a login.')

    async def test_archived_deleted_dialog_restores_and_deletes(self):
        from reclaim.core import lifecycle
        from reclaim.core.model import Chat, Turn
        db = self.root / 'chats'
        gui.database.init(db)
        pdir = db / 'ChatGPT'
        pdir.mkdir()
        sync = lifecycle.SyncState(pdir, 'chatgpt')
        for cid, name, state in (('a1', 'Arch Chat', 'archived'), ('d1', 'Del Chat', 'deleted')):
            stats = gui.lifecycle.writer.write_chat(
                Chat(cid, name, f'https://x/{cid}', [Turn('user', 'hi')], provider='chatgpt'),
                pdir, updated_at='1')
            sync.mark(cid, '1', str(stats['md']), title=name)
            lifecycle.move_chat_folder(pdir, sync, cid, state, reason=state)

        await self.window._archived_deleted_dialog()
        dialog = self.page.dialog
        assert dialog is not None and dialog.actions is not None
        dialog.content.content.controls[0].controls[0].value = True  # select first item
        await dialog.actions[1].on_click(None)  # Restore selected
        self.assertIn('Restored 1 chat', self.window.summary.value)
        self.assertEqual(len(lifecycle.list_state_chats(db)), 1)

        await self.window._archived_deleted_dialog()
        dialog = self.page.dialog
        assert dialog is not None and dialog.actions is not None
        dialog.content.content.controls[0].controls[0].value = True  # select remaining deleted item
        dialog.actions[2].on_click(None)  # Delete permanently -> confirmation dialog
        confirm = self.page.dialog
        assert confirm is not None and confirm.actions is not None
        self.assertEqual(confirm.title, 'Delete permanently?')
        await confirm.actions[1].on_click(None)
        self.assertIn('Permanently deleted 1', self.window.summary.value)
        self.assertEqual(lifecycle.list_state_chats(db), [])

    async def test_plain_labels_version_release_notice_and_separate_check_status(self):
        self.assertEqual(self.window.dry_run.content, 'Check for updates')
        self.assertEqual(self.window.rebuild.content, 'Download everything again')
        self.assertTrue(bool(self.window.version_text.value))
        self.assertFalse(self.window.release_notice.visible)

        with patch.object(gui.releases, 'app_version', return_value='3.3.4'):
            await self.window.check_release_update(
                fetcher=lambda _u: {'tag_name': 'v3.4.0',
                                    'html_url': 'https://example.test/v3.4.0'})
        self.assertTrue(self.window.release_notice.visible)
        self.assertIn('v3.4.0', self.window.release_notice_text.value)

        info = gui.status.ProviderStatus(
            provider='chatgpt', dir=str(self.root / 'chats' / 'ChatGPT'),
            present=True, chats=2, chat_folders=2,
            last_sync='2026-11-01 10:00:00',
            last_checked='2026-11-17 12:00:00')
        text = gui.status_text(info)
        self.assertIn('last download 2026-11-01 10:00:00', text)
        self.assertIn('last checked 2026-11-17 12:00:00', text)

    async def test_failure_is_visible(self):
        self.controller.fail = True
        await self.window._update_selected()
        self.assertIn('completed with errors', self.window.summary.value)
        self.assertIn('Synthetic failure', self.window.log.value)

    async def test_stop_button_stops_running_update_after_current_chat(self):
        self.assertTrue(self.window.stop_button.disabled)
        seen_enabled = []

        class _InteractiveStopController(_Controller):
            def __init__(self, window: gui.ReclaimWindow):
                super().__init__()
                self.window = window

            def run(self, providers, mode='update', sink=None, *, log=False,
                    only_ids=None):
                assert sink is not None
                seen_enabled.append(not self.window.stop_button.disabled)
                sink(ProgressEvent('run_started', total=len(providers), message=mode))
                sink(ProgressEvent('provider_started', providers[0], current=1,
                                   total=len(providers)))
                self.window._stop_run()
                sink(ProgressEvent('stopped', providers[0], current=1, total=2,
                                   message='Stopped after 1 of 2 chats'))
                sink(ProgressEvent('provider_completed', providers[0], current=1,
                                   total=len(providers), message='exit 0'))
                sink(ProgressEvent('run_completed', current=len(providers),
                                   total=len(providers), message='exit 0'))
                return RunSummary(((providers[0], 0),))

        controller = _InteractiveStopController(self.window)
        self.window.controller = controller
        for name, check in self.window.checks.items():
            check.value = name == 'chatgpt'
        await self.window._update_selected()
        self.assertEqual(seen_enabled, [True])
        self.assertTrue(controller.stopped)
        self.assertTrue(self.window.stop_button.disabled)
        self.assertTrue(self.window.summary.value.startswith('Update stopped · ChatGPT: ok'))
        self.assertIn('Stopped after 1 of 2 chats', self.window.log.value)

        # Before refresh_archive runs (or in dry-run), completed event keeps the Stopped row.
        self.window._event(ProgressEvent('stopped', 'kimi', message='Stopped after 1 of 2 chats'))
        self.window._event(ProgressEvent('provider_completed', 'kimi', current=1, total=1,
                                         message='exit 0'))
        self.assertEqual(self.window.provider_status['kimi'].value, 'Stopped after 1 of 2 chats')


class _RaisingController(_Controller):
    def __init__(self, exc):
        super().__init__()
        self.exc = exc

    def run(self, providers, mode='update', sink=None, *, log=False,
            only_ids=None):
        self.calls.append((list(providers), mode))
        raise self.exc


class _WarningController(_Controller):
    def run(self, providers, mode='update', sink=None, *, log=False,
            only_ids=None):
        assert sink is not None
        sink(ProgressEvent('warning', providers[0], 'Example', message='raw.json was not saved'))
        return super().run(providers, mode, sink, log=log, only_ids=only_ids)


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
    def run(self, providers, mode='update', sink=None, *, log=False,
            only_ids=None):
        self.calls.append((list(providers), mode))
        assert sink is not None
        for name in providers:
            sink(ProgressEvent('checked', name, message='logged in · 3 chats listed'))
        return RunSummary(tuple((name, 0) for name in providers))


class _Clipboard:
    copied: ClassVar[list[str]] = []  # what the fake clipboard received

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

    def dialog_actions(self):
        dialog = self.page.dialog
        assert dialog is not None
        return {a.key: a for a in dialog.actions}

    def dialog_text(self):
        dialog = self.page.dialog
        assert dialog is not None
        texts = []

        def walk(control):
            if isinstance(control, gui.ft.Text) and control.value:
                texts.append(control.value)
            for attr in ('controls', 'content'):
                child = getattr(control, attr, None)
                if isinstance(child, list):
                    for item in child:
                        walk(item)
                elif child is not None:
                    walk(child)

        walk(dialog.content)
        return ' '.join(texts)

    def dialog_control(self, key: str) -> Any:
        dialog = self.page.dialog
        assert dialog is not None
        pending = [dialog.content]
        while pending:
            control = pending.pop()
            if getattr(control, 'key', None) == key:
                return control
            for attr in ('controls', 'content'):
                child = getattr(control, attr, None)
                if isinstance(child, list):
                    pending.extend(child)
                elif child is not None:
                    pending.append(child)
        raise AssertionError(f'missing control {key}')

    async def test_panel_shows_checks_and_runs_the_live_check(self):
        await self.window._diagnostics()
        self.assertIn('Settings: defaults (no settings file)', self.dialog_text())
        actions = self.dialog_actions()
        await actions['diagnostics-live'].on_click(None)
        self.assertEqual(self.controller.calls[0][1], 'check')
        self.assertIn('Live check completed successfully', self.window.summary.value)
        self.assertIn('ChatGPT: logged in · 3 chats listed', self.dialog_text())
        self.assertEqual(self.window.provider_status['kimi'].value,
                         'Live check: logged in · 3 chats listed')

    async def test_copy_diagnostics(self):
        await self.window._diagnostics()
        actions = self.dialog_actions()
        with patch.object(gui.ft, 'Clipboard', _Clipboard):
            await actions['diagnostics-copy'].on_click(None)
        self.assertIn('Reclaim My Chats diagnostics', _Clipboard.copied[-1])
        self.assertIn('Diagnostics copied', self.window.summary.value)

    async def test_capture_a_chat_runs_controller_and_shows_open_folder(self):
        await self.window._diagnostics()
        provider = self.dialog_control('capture-provider')
        url = self.dialog_control('capture-url')
        capture_btn = self.dialog_control('diagnostics-capture')
        self.assertFalse(capture_btn.disabled)
        provider.value = 'claude'
        url.value = 'https://claude.ai/chat/1234'
        await capture_btn.on_click(None)
        self.assertEqual(self.controller.capture_calls,
                         [('claude', 'https://claude.ai/chat/1234', None)])
        self.assertIn('Capture completed successfully', self.window.summary.value)
        self.assertEqual(self.window.provider_status['claude'].value,
                         'Capture: 1 response recorded')
        saved = self.dialog_control('capture-saved')
        self.assertIn('claude-capture.json', saved.value)
        open_btn = self.dialog_control('capture-open-folder')
        with patch.object(gui, 'open_directory') as opener:
            open_btn.on_click(None)
        opener.assert_called_once_with(Path(tempfile.gettempdir()))

    async def test_capture_disabled_when_blocked_and_requires_url(self):
        self.window.blocked = True
        await self.window._diagnostics()
        self.assertTrue(self.dialog_control('diagnostics-capture').disabled)
        self.window.blocked = False
        await self.window._diagnostics()
        await self.dialog_control('diagnostics-capture').on_click(None)
        self.assertEqual(self.controller.capture_calls, [])
        assert self.page.dialog is not None
        self.assertEqual(self.page.dialog.title, 'Error')


@unittest.skipUnless(gui.ft is not None, 'Flet GUI extra is not installed')
class TestGuiLoginRequest(unittest.IsolatedAsyncioTestCase):
    async def test_login_request_shows_in_the_status_and_the_provider_row(self):
        with tempfile.TemporaryDirectory() as temp:
            page = _Page()
            window = gui.ReclaimWindow(page, Path(temp), _Controller())
            await window.refresh_archive()
            request = 'Log in to Kimi in the browser window. Waiting up to 15 minutes...'
            window._event(ProgressEvent('provider_started', 'kimi', current=1, total=1))
            window._event(ProgressEvent('login_needed', 'kimi', message=request))
            self.assertEqual(window.current.value, f'Kimi: {request}')
            self.assertEqual(window.provider_status['kimi'].value,
                             'Waiting for login in the browser window')
            self.assertIn(f'Kimi: {request}', window.log.value)
            window._event(ProgressEvent('logged_in', 'kimi', message='Logged in.'))
            self.assertEqual(window.provider_status['kimi'].value, 'Running')
            window._event(ProgressEvent('chat_started', 'kimi', 'Example', 1, 2))
            self.assertNotIn('Example', window.log.value)  # progress, not Details

    async def test_login_banner_provider_table_and_first_run_setup(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            page = _Page()
            controller = _Controller()
            window = gui.ReclaimWindow(page, root, controller)
            await window.refresh_archive()
            self.assertTrue(window.setup_banner.visible)

            # Login banner + Bring browser forward button
            window._event(ProgressEvent('login_needed', 'chatgpt', message='Log in to ChatGPT...'))
            self.assertTrue(window.login_banner.visible)
            self.assertEqual(window.provider_chips['chatgpt'].value, 'Needs login')
            bring_handler: Any = window.bring_browser_button.on_click
            assert bring_handler is not None
            bring_handler(None)
            self.assertTrue(controller.brought_forward)
            window._event(ProgressEvent('logged_in', 'chatgpt', message='Logged in.'))
            self.assertFalse(window.login_banner.visible)

            # Per-row Log in & Review buttons
            await window.provider_login_buttons['chatgpt'].on_click(None)
            self.assertEqual(controller.calls[-1], (['chatgpt'], 'check'))
            await window.provider_review_buttons['chatgpt'].on_click(None)
            self.assertEqual(controller.calls[-1], (['chatgpt'], 'dry-run'))

            # First-run setup wizard (chrome check + save + login)
            with patch.object(gui.browser, 'find_chrome', return_value='/usr/bin/chrome'):
                window._setup_wizard()
            dialog = page.dialog
            assert dialog is not None and dialog.actions is not None
            controls = dialog.content.content.controls
            self.assertIn('/usr/bin/chrome', controls[0].value)
            await dialog.actions[2].on_click(None)  # Save & log in
            self.assertTrue((root / 'chats' / gui.database.MARKER).exists())
            self.assertTrue((root / '.reclaim.json').exists())
            self.assertFalse(window.setup_banner.visible)
            self.assertEqual(controller.calls[-1][1], 'check')


class TestOptionalGui(unittest.TestCase):
    def test_cli_entry_explains_missing_extra(self):
        stderr = StringIO()
        with patch.object(gui, 'ft', None), patch.object(gui.sys, 'stderr', stderr):
            self.assertEqual(gui.main(), 2)
        self.assertIn('reclaimmychats[gui]', stderr.getvalue())
