"""Diagnostics (core/doctor.py), the live check mode and `reclaim doctor`. Offline."""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from reclaim import __main__ as cli
from reclaim.core import config, database, doctor, locks
from reclaim.core.events import ProgressEvent
from reclaim.core.service import ArchiveController
from reclaim.core.writer import write_chat
from reclaim.core.model import Chat, Turn


class _Context:
    def close(self):
        pass


class _Playwright:
    def __enter__(self):
        return object()

    def __exit__(self, *_):
        return False


class _Spec:
    name = 'chatgpt'
    noun = 'conversations'

    def open(self, page):
        return None

    def listing(self, session):
        return [object(), object()]


class _Module:
    SPEC = _Spec()

    @staticmethod
    def parse_args(argv):
        from argparse import Namespace
        return Namespace(output_dir=argv[argv.index('-o') + 1], dry_run=True, list=False)

    @staticmethod
    def run_session(*_args, **_kwargs):
        raise AssertionError('a live check must not run an update')


class TestDoctor(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)

    def tearDown(self):
        self._temp.cleanup()

    def by_area(self, checks):
        return {c.area: c for c in checks}

    def test_first_use_is_in_order(self):
        with patch.object(doctor.browser, 'find_chrome', return_value='/usr/bin/chrome'):
            checks = self.by_area(doctor.run_checks(self.root))
        self.assertEqual(checks['Database'].status, doctor.INFO)  # created by the first update
        self.assertEqual(checks['Browser'].status, doctor.OK)
        self.assertFalse(any(c.status == doctor.PROBLEM for c in checks.values()))

    def test_problems_come_with_advice(self):
        (self.root / '.reclaim.json').write_text('{"archive": 42}')
        with (patch.object(doctor.browser, 'find_chrome', return_value=None),
              patch.object(doctor.browser, 'find_playwright_chromium', return_value=None)):
            checks = doctor.run_checks(self.root)
        areas = self.by_area(checks)
        self.assertEqual(areas['Settings'].status, doctor.PROBLEM)
        self.assertIn('Reset settings', areas['Settings'].advice)
        self.assertEqual(areas['Database'].detail, 'Not checked until the settings are fixed.')
        self.assertEqual(areas['Browser'].status, doctor.WARNING)
        text = doctor.report(checks)
        self.assertIn('PROBLEM  Settings', text)
        self.assertIn('→ Install Google Chrome', text)

    def test_playwright_chromium_fallback_is_reported_as_ok(self):
        with (patch.object(doctor.browser, 'find_chrome', return_value=None),
              patch.object(doctor.browser, 'find_playwright_chromium',
                           return_value='/cache/ms-playwright/chromium-1248/chrome-linux64/chrome')):
            checks = self.by_area(doctor.run_checks(self.root))
        self.assertEqual(checks['Browser'].status, doctor.OK)
        self.assertIn('Playwright Chromium', checks['Browser'].detail)
        self.assertIn('RECLAIM_CHROME_PATH', checks['Browser'].advice)

    def test_find_playwright_chromium_reads_cache(self):
        exe = self.root / 'ms-playwright' / 'chromium-9999' / 'chrome-linux64' / 'chrome'
        exe.parent.mkdir(parents=True)
        exe.write_text('stub')
        with (patch.dict(os.environ, {'PLAYWRIGHT_BROWSERS_PATH': str(self.root / 'ms-playwright')}),
              patch.object(doctor.browser, '_playwright_chromium_revision', return_value='9999')):
            self.assertEqual(doctor.browser.find_playwright_chromium(), str(exe))
        with (patch.dict(os.environ, {'PLAYWRIGHT_BROWSERS_PATH': str(self.root / 'empty')}),
              patch.object(doctor.browser, '_playwright_chromium_revision', return_value='9999')):
            self.assertIsNone(doctor.browser.find_playwright_chromium())

    def test_find_playwright_chromium_supports_frozen_and_relative_env_path(self):
        frozen_pkg = self.root / 'meipass' / 'playwright'
        frozen_pkg.mkdir(parents=True)
        with patch.object(doctor.browser.sys, '_MEIPASS', str(self.root / 'meipass'), create=True):
            self.assertEqual(doctor.browser._playwright_package_dir(), frozen_pkg)

        exe = self.root / 'rel-cache' / 'chromium-9999' / 'chrome-linux64' / 'chrome'
        exe.parent.mkdir(parents=True)
        exe.write_text('stub')
        with (patch.dict(os.environ, {'HOME': str(self.root),
                                      'USERPROFILE': str(self.root),
                                      'PLAYWRIGHT_BROWSERS_PATH': '~/rel-cache'}),
              patch.object(doctor.browser, '_playwright_chromium_revision', return_value='9999')):
            self.assertEqual(doctor.browser.find_playwright_chromium(), str(exe.resolve()))

    def test_windows_find_chrome_checks_localappdata_and_edge_fallback(self):
        edge = r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe'
        local_chrome = str(self.root / 'Google' / 'Chrome' / 'Application' / 'chrome.exe')
        existing = {edge, local_chrome}

        def fake_exists(path_obj: Path) -> bool:
            return str(path_obj) in existing

        with (patch.dict(os.environ, {'LOCALAPPDATA': str(self.root)}, clear=False),
              patch.object(doctor.browser.os, 'name', 'nt'),
              patch.object(doctor.browser.Path, 'exists', autospec=True, side_effect=fake_exists),
              patch.object(doctor.browser.shutil, 'which', return_value=None)):
            os.environ.pop('RECLAIM_CHROME_PATH', None)
            self.assertEqual(doctor.browser.find_chrome(self.root), local_chrome)
            existing.remove(local_chrome)
            self.assertEqual(doctor.browser.find_chrome(self.root), edge)

    def test_database_locks_and_synced_login_data(self):
        db = self.root / 'chats'
        write_chat(Chat(id='a', title='A', source_url='', provider='test',
                        turns=[Turn('user', 'q')]), db / 'ChatGPT')
        database.init(db)
        (db / locks.LOCK_NAME).write_text(json.dumps({'host': 'laptop-elsewhere', 'started': 'noon'}))
        profile = self.root / 'Sync' / 'profile'
        (self.root / 'Sync' / '.stfolder').mkdir(parents=True)
        profile.mkdir()
        os.chmod(profile, 0o755)
        config.save(self.root, providers=['chatgpt'], archive=None, profile=str(profile))
        checks = doctor.run_checks(self.root)
        areas = {(c.area, c.status) for c in checks}
        self.assertIn(('Database', doctor.OK), areas)
        self.assertIn(('Database lock', doctor.WARNING), areas)
        details = ' '.join(c.detail for c in checks if c.area == 'Login data')
        self.assertIn('synced by Syncthing', details)
        if os.name != 'nt':
            self.assertIn('Other users of this computer can read it.', details)

    def test_live_check_lists_without_writing(self):
        events: list[ProgressEvent] = []
        with (patch('reclaim.core.service.provider_module', return_value=_Module()),
              patch('reclaim.core.service.browser.launch', return_value=(_Context(), object())),
              patch('playwright.sync_api.sync_playwright', return_value=_Playwright()),
              contextlib.redirect_stdout(io.StringIO())):
            summary = ArchiveController(self.root).run(['chatgpt'], 'check', events.append)
        self.assertEqual(summary.exit_code, 0)
        self.assertFalse((self.root / 'chats').exists())  # no database, no records
        checks = doctor.checks_from_events(events)
        self.assertEqual([(c.area, c.status, c.detail) for c in checks],
                         [('chatgpt', doctor.OK, 'logged in · 2 conversations listed')])
        failed = doctor.checks_from_events([ProgressEvent('error', 'kimi', message='HTTP 401')])
        self.assertEqual(failed[0].status, doctor.PROBLEM)

    def test_command_exit_code_follows_problems(self):
        out = io.StringIO()
        with (patch.dict(os.environ, {'RECLAIM_HOME': str(self.root)}),
              patch.object(doctor.browser, 'find_chrome', return_value='/usr/bin/chrome'),
              contextlib.redirect_stdout(out)):
            self.assertEqual(cli.main(['doctor']), 0)
            (self.root / '.reclaim.json').write_text('{bad')
            self.assertEqual(cli.main(['doctor']), 1)
        self.assertIn('Reclaim My Chats diagnostics', out.getvalue())


if __name__ == '__main__':
    unittest.main()
