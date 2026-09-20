"""Offline tests for the shared native-provider application boundary."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from reclaim.core import browser, config
from reclaim.core.events import ProgressEvent
from reclaim.core.manifest import SyncState, print_dry_run
from reclaim.core.service import ArchiveController, BusyOperationError, RunSummary


class TestApplicationConfig(unittest.TestCase):
    def test_settings_round_trip_preserves_unknown_keys(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / '.reclaim.json').write_text(json.dumps({'future': 42}))
            config.save(root, providers=['chatgpt'], archive='archive', profile='old-profile')
            self.assertEqual(config.load(root), {
                'future': 42, 'providers': ['chatgpt'],
                'archive': 'archive', 'profile': 'old-profile',
            })
            self.assertEqual(config.archive_root(root), root / 'archive')
            self.assertEqual(config.profile_root(root), root / 'old-profile')

    def test_invalid_settings_are_not_overwritten(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / '.reclaim.json'
            path.write_text('{bad json')
            with self.assertRaises(ValueError):
                config.save(root, providers=['kimi'], archive='chats')
            self.assertEqual(path.read_text(), '{bad json')
            with self.assertRaises(ValueError):
                config.save(root, providers=[], archive='chats')

    def test_home_override_preserves_existing_checkout(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch.dict('os.environ', {'RECLAIM_HOME': temp}):
                self.assertEqual(config.default_root(), Path(temp))

    def test_frozen_app_uses_writable_data_home(self):
        with tempfile.TemporaryDirectory() as temp:
            with (patch.object(config.sys, 'frozen', True, create=True),
                  patch.dict('os.environ', {'XDG_DATA_HOME': temp}, clear=True)):
                if config.os.name == 'posix' and config.sys.platform == 'linux':
                    self.assertEqual(config.default_root(), Path(temp) / 'ReclaimMyChats')

    def test_missing_browser_has_an_actionable_error(self):
        with tempfile.TemporaryDirectory() as temp:
            fake = SimpleNamespace(chromium=SimpleNamespace(
                executable_path=str(Path(temp) / 'missing-chromium')))
            with patch('reclaim.core.browser.find_chrome', return_value=None):
                with self.assertRaisesRegex(browser.BrowserUnavailableError,
                                            'Install Chrome or Chromium'):
                    browser.launch(fake, profile_dir=Path(temp) / 'profile')


class TestProgressEvents(unittest.TestCase):
    def test_dry_run_emits_structured_plan_without_writing(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = SyncState(root, 'chatgpt', migrate=False)
            state.mark('one', '1', 'one.md')
            events: list[ProgressEvent] = []
            chats = [{'id': 'one', 'title': 'Unchanged'},
                     {'id': 'two', 'title': 'New'}]
            print_dry_run(chats, {'one': '1', 'two': '2'}, state, True,
                          sink=events.append, provider='chatgpt')
            self.assertEqual([(e.kind, e.message, e.current, e.total)
                              for e in events], [
                                  ('chat_planned', 'skip (unchanged)', 1, 2),
                                  ('chat_planned', 'fetch (new)', 2, 2),
                              ])
            self.assertEqual(list(root.iterdir()), [])


class _FakeContext:
    closed = False

    def close(self):
        self.closed = True


class _FakePlaywright:
    def __enter__(self):
        return object()

    def __exit__(self, *_):
        return False


class _FakeProvider:
    def __init__(self, exit_code=0):
        self.argv = None
        self.exit_code = exit_code

    def parse_args(self, argv):
        self.argv = argv
        return argv

    def run_session(self, page, args, sink=None):
        if sink:
            sink(ProgressEvent('chat_completed', 'chatgpt', 'Example', 1, 1))
        return self.exit_code


class TestArchiveController(unittest.TestCase):
    def test_mode_translation_and_events(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            controller = ArchiveController(root)
            fake = _FakeProvider()
            ctx = _FakeContext()
            events: list[ProgressEvent] = []
            with (patch('reclaim.core.service.provider_module', return_value=fake),
                  patch('reclaim.core.service.browser.launch', return_value=(ctx, object())),
                  patch('playwright.sync_api.sync_playwright', return_value=_FakePlaywright())):
                summary = controller.run(['chatgpt'], 'dry-run', events.append)
            self.assertIsInstance(summary, RunSummary)
            self.assertEqual(summary.exit_code, 0)
            assert fake.argv is not None
            self.assertIn('--dry-run', fake.argv)
            self.assertEqual(fake.argv[fake.argv.index('-o') + 1],
                             str(root / 'chats' / 'ChatGPT'))
            self.assertEqual([e.kind for e in events], [
                'run_started', 'provider_started', 'chat_completed',
                'provider_completed', 'run_completed',
            ])
            self.assertTrue(ctx.closed)

    def test_busy_operation_rejected(self):
        controller = ArchiveController()
        self.assertTrue(controller._lock.acquire(blocking=False))
        try:
            with self.assertRaises(BusyOperationError):
                controller.run(['chatgpt'])
        finally:
            controller._lock.release()

    def test_invalid_selection_rejected_before_browser(self):
        controller = ArchiveController()
        for names in ([], ['unknown']):
            with self.assertRaises(ValueError):
                controller.run(names)
