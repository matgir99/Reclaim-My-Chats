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
        with patch.object(doctor.browser, 'find_chrome', return_value=None):
            checks = doctor.run_checks(self.root)
        areas = self.by_area(checks)
        self.assertEqual(areas['Settings'].status, doctor.PROBLEM)
        self.assertIn('Reset settings', areas['Settings'].advice)
        self.assertEqual(areas['Database'].detail, 'Not checked until the settings are fixed.')
        self.assertEqual(areas['Browser'].status, doctor.WARNING)
        text = doctor.report(checks)
        self.assertIn('PROBLEM  Settings', text)
        self.assertIn('→ Install Google Chrome', text)

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
