"""Phase 1 safeguards: atomic writes, sync records, the database marker,
locks, the log file and the checks every run passes. Offline, synthetic."""

from __future__ import annotations

import contextlib
import io
import json
import os
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from reclaim import __main__ as cli
from reclaim.core import config, database, fsutil, locks, logs
from reclaim.core.events import ProgressEvent
from reclaim.core.manifest import SyncState, rebuild_records, save_chat
from reclaim.core.model import Chat, Turn
from reclaim.core.service import ArchiveController, offline_session
from reclaim.core.writer import write_chat


def chat(cid: str, title: str = 'Title', text: str = 'hello') -> Chat:
    return Chat(id=cid, title=title, source_url=f'https://example.test/{cid}', provider='test',
                turns=[Turn('user', text), Turn('model', 'answer')])


class Temp(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._temp.name)

    def tearDown(self):
        self._temp.cleanup()


class TestAtomicWrites(Temp):
    def test_replaces_content_and_leaves_no_temporary_file_on_failure(self):
        target = self.tmp / 'chat.json'
        fsutil.atomic_write_text(target, 'first')
        fsutil.atomic_write_text(target, 'second')
        self.assertEqual(target.read_text(), 'second')
        with patch('reclaim.core.fsutil.os.replace', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                fsutil.atomic_write_text(target, 'third')
        self.assertEqual(target.read_text(), 'second')
        self.assertEqual([p.name for p in self.tmp.iterdir()], ['chat.json'])


class TestSyncRecords(Temp):
    def test_each_chat_is_recorded_at_once(self):
        # An interrupted run keeps the records of the chats it saved.
        out = self.tmp / 'ChatGPT'
        sync = SyncState(out, 'chatgpt')
        for cid in ('a', 'b'):
            stats = write_chat(chat(cid, f'Chat {cid}'), out, updated_at=f'{cid}-1')
            sync.mark(cid, f'{cid}-1', str(stats['md']))
        reopened = SyncState(out, 'chatgpt')  # no save() was called
        self.assertEqual(reopened.classify('a', 'a-1'), 'unchanged')
        self.assertEqual(reopened.classify('b', 'b-1'), 'unchanged')

    def test_damaged_record_is_kept_aside_and_rebuilt_from_chat_folders(self):
        out = self.tmp / 'ChatGPT'
        write_chat(chat('a', 'Alpha'), out, updated_at='t1')
        write_chat(chat('b', 'Beta'), out / 'Project', updated_at=None)
        (out / '.last_sync_chatgpt.json').write_text('{"a": {"updated_at": "t1", "md": "Al')
        with contextlib.redirect_stdout(io.StringIO()) as printed:
            sync = SyncState(out, 'chatgpt')
        self.assertIn('Rebuilt 2 records', printed.getvalue())
        self.assertEqual(sync.classify('a', 't1'), 'unchanged')
        self.assertEqual(sync.known('b'), {'updated_at': None, 'md': 'Project/Beta/Beta.md'})
        backups = list(out.glob('.last_sync_chatgpt.json.damaged-*'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(json.loads((out / '.last_sync_chatgpt.json').read_text())['a']['md'],
                         'Alpha/Alpha.md')

    def test_missing_record_is_rebuilt_and_previews_never_write(self):
        out = self.tmp / 'Kimi Chat'
        write_chat(chat('k', 'Kay'), out, updated_at=7)
        with contextlib.redirect_stdout(io.StringIO()):
            preview = SyncState(out, 'kimi', migrate=False)
        self.assertEqual(preview.classify('k', 7), 'unchanged')
        preview.mark('other', 1, 'x.md')
        self.assertFalse((out / '.last_sync_kimi.json').exists())
        with contextlib.redirect_stdout(io.StringIO()):
            SyncState(out, 'kimi')
        self.assertTrue((out / '.last_sync_kimi.json').exists())

    def test_rebuild_prefers_the_most_recent_copy(self):
        out = self.tmp / 'Claude'
        old = write_chat(chat('c', 'Old name'), out, updated_at='1')
        new = write_chat(chat('c', 'New name'), out, updated_at='2')
        os.utime(old['dir'] / 'chat.json', (1, 1))
        self.assertEqual(rebuild_records(out)['c'],
                         {'updated_at': '2', 'md': 'New name/New name.md'})
        self.assertTrue(new['md'].exists())


class TestSaveChat(Temp):
    def test_failed_raw_write_is_reported_and_the_chat_still_counts(self):
        out = self.tmp / 'DeepSeek'
        sync = SyncState(out, 'deepseek')
        events: list[ProgressEvent] = []
        with (patch('reclaim.core.manifest.write_raw', side_effect=OSError('disk full')),
              contextlib.redirect_stdout(io.StringIO()) as printed):
            stats = save_chat(chat('d', 'Dee'), out, sync, 'd', 5, {'raw': True},
                              sink=events.append, provider='deepseek')
        self.assertEqual(stats['warnings'], ['raw.json was not saved: disk full'])
        self.assertIn('warning: Dee: raw.json was not saved', printed.getvalue())
        self.assertEqual([(e.kind, e.provider, e.title) for e in events],
                         [('warning', 'deepseek', 'Dee')])
        self.assertEqual(SyncState(out, 'deepseek').classify('d', 5), 'unchanged')


class TestDatabase(Temp):
    def test_classifies_folders_without_changing_them(self):
        kinds = {}
        kinds['missing'] = self.tmp / 'missing'
        (self.tmp / 'empty').mkdir()
        (self.tmp / 'empty' / '.stfolder').mkdir()  # Syncthing marker only
        kinds['empty'] = self.tmp / 'empty'
        (self.tmp / 'documents').mkdir()
        (self.tmp / 'documents' / 'letter.txt').write_text('x')
        kinds['foreign'] = self.tmp / 'documents'
        legacy = self.tmp / 'legacy' / 'ChatGPT'
        legacy.mkdir(parents=True)
        (legacy / '.last_sync_chatgpt.json').write_text('{}')
        kinds['legacy'] = self.tmp / 'legacy'
        (self.tmp / 'file').write_text('x')
        kinds['file'] = self.tmp / 'file'
        for kind, path in kinds.items():
            self.assertEqual(database.inspect(path).kind, kind, path)
        self.assertFalse((self.tmp / 'legacy' / database.MARKER).exists())

    def test_unavailable_link_target_counts_as_missing_and_is_never_created(self):
        if os.name == 'nt':
            self.skipTest('symbolic links need extra rights on Windows')
        link = self.tmp / 'chats'
        link.symlink_to(self.tmp / 'unmounted' / 'Reclaimed Chats')
        self.assertEqual(database.inspect(link).kind, 'missing')
        with self.assertRaisesRegex(database.DatabaseError, 'not available'):
            database.init(link)
        self.assertFalse((self.tmp / 'unmounted').exists())

    def test_init_marks_a_new_database_and_refuses_unrelated_folders(self):
        state = database.init(self.tmp / 'new')
        self.assertEqual(state.kind, 'ready')
        marker = json.loads((self.tmp / 'new' / database.MARKER).read_text())
        self.assertEqual(marker['format'], database.DATABASE_FORMAT)
        (self.tmp / 'other').mkdir()
        (self.tmp / 'other' / 'photo.jpg').write_bytes(b'x')
        with self.assertRaisesRegex(database.DatabaseError, 'contains other files'):
            database.init(self.tmp / 'other')
        (self.tmp / 'newer').mkdir()
        (self.tmp / 'newer' / database.MARKER).write_text('{"format": 99}')
        with self.assertRaisesRegex(database.DatabaseError, 'newer version'):
            database.prepare(self.tmp / 'newer')

    def test_adopting_an_older_database_records_provider_timestamps(self):
        db = self.tmp / 'db'
        out = db / 'ChatGPT'
        stats = write_chat(chat('a', 'Alpha'), out)
        payload = json.loads((stats['dir'] / 'chat.json').read_text())
        for key in ('format', 'provider_updated_at', 'state', 'markdown', 'saved_at'):
            payload.pop(key)  # as written by versions before November 2026
        (stats['dir'] / 'chat.json').write_text(json.dumps(payload))
        old_path = '/old/checkout/chats/ChatGPT/Alpha/Alpha.md'
        (out / '.last_sync_chatgpt.json').write_text(
            json.dumps({'a': {'updated_at': '2026-08-01', 'md': old_path}}))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(database.prepare(db, readonly=True).kind, 'legacy')
            self.assertFalse((db / database.MARKER).exists())
            self.assertEqual(database.prepare(db).kind, 'ready')
        updated = json.loads((stats['dir'] / 'chat.json').read_text())
        self.assertEqual((updated['format'], updated['provider_updated_at'],
                          updated['state'], updated['markdown']),
                         (1, '2026-08-01', 'active', 'Alpha.md'))
        self.assertEqual(updated['turns'], payload['turns'])
        self.assertEqual(list(updated)[-1], 'turns')

    def test_old_absolute_record_in_a_project_named_like_the_provider(self):
        # A ChatGPT project called "ChatGPT" puts chats in ChatGPT/ChatGPT/<chat>.
        db = self.tmp / 'db'
        out = db / 'ChatGPT'
        project_chat = chat('p', 'Prompt')
        project_chat.project = 'ChatGPT'
        stats = write_chat(project_chat, out)
        payload = json.loads((stats['dir'] / 'chat.json').read_text())
        payload.pop('provider_updated_at')
        (stats['dir'] / 'chat.json').write_text(json.dumps(payload))
        old = '/home/user/old place/ChatGPT/ChatGPT/Prompt/Prompt.md'
        (out / '.last_sync_chatgpt.json').write_text(json.dumps({'p': {'updated_at': 9, 'md': old}}))
        self.assertEqual(SyncState(out, 'chatgpt')._resolve(old), stats['md'])
        with contextlib.redirect_stdout(io.StringIO()):
            database.prepare(db)
        self.assertEqual(json.loads((stats['dir'] / 'chat.json').read_text())['provider_updated_at'], 9)

    def test_only_the_default_location_is_created_on_first_use(self):
        with self.assertRaisesRegex(database.DatabaseError, 'was not found'):
            database.prepare(self.tmp / 'custom')
        self.assertEqual(database.prepare(self.tmp / 'default', create=True, readonly=True).kind,
                         'missing')
        self.assertFalse((self.tmp / 'default').exists())
        self.assertEqual(database.prepare(self.tmp / 'default', create=True).kind, 'ready')


class TestLocks(Temp):
    def test_second_run_is_refused_and_the_lock_is_released(self):
        with locks.hold(self.tmp, 'update'):
            with self.assertRaises(locks.LockedError) as caught:
                locks.acquire(self.tmp, 'update')
            self.assertFalse(caught.exception.removable)  # this process is running
            self.assertIn('this computer', str(caught.exception))
        self.assertIsNone(locks.read_holder(self.tmp))

    def test_lock_of_an_ended_process_here_is_cleared(self):
        (self.tmp / locks.LOCK_NAME).write_text(json.dumps(
            {'host': socket.gethostname(), 'pid': 999_999, 'started': 'earlier'}))
        with patch('reclaim.core.locks._pid_alive', return_value=False):
            mine = locks.acquire(self.tmp, 'update')
        self.assertEqual(locks.read_holder(self.tmp), mine)
        locks.release(self.tmp, mine)

    def test_lock_of_another_computer_needs_an_explicit_removal(self):
        (self.tmp / locks.LOCK_NAME).write_text(json.dumps(
            {'host': 'laptop-elsewhere', 'pid': 1, 'started': '2026-11-09 08:00:00',
             'purpose': 'update'}))
        with self.assertRaises(locks.LockedError) as caught:
            locks.acquire(self.tmp, 'update')
        self.assertTrue(caught.exception.removable)
        self.assertIn('laptop-elsewhere since 2026-11-09 08:00:00', str(caught.exception))
        removed = locks.remove(self.tmp)
        assert removed is not None
        self.assertEqual(removed['host'], 'laptop-elsewhere')
        locks.release(self.tmp, locks.acquire(self.tmp, 'update'))

    def test_release_never_removes_someone_elses_lock(self):
        mine = locks.acquire(self.tmp, 'update')
        locks.remove(self.tmp)
        other = locks.acquire(self.tmp, 'update')
        locks.release(self.tmp, mine)
        self.assertEqual(locks.read_holder(self.tmp), other)


class TestLog(Temp):
    def test_printed_output_and_errors_reach_the_log_and_streams_are_restored(self):
        out, err = sys.stdout, sys.stderr
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(RuntimeError):
                with logs.capture(self.tmp, 'update chatgpt'):
                    print('[1/2] Example -> 2t, 10 chars')
                    print('partial line without newline', end='')
                    raise RuntimeError('listing failed')
        self.assertIs(sys.stderr, err)
        self.assertIs(sys.stdout, out)
        text = (self.tmp / 'logs' / 'reclaim.log').read_text()
        for expected in ('update chatgpt: started', '[1/2] Example -> 2t, 10 chars',
                         'partial line without newline', 'RuntimeError: listing failed',
                         'update chatgpt: finished'):
            self.assertIn(expected, text)


class _Context:
    def close(self):
        pass


class _Playwright:
    def __enter__(self):
        return object()

    def __exit__(self, *_):
        return False


class _Provider:
    def __init__(self, code=0):
        self.code = code
        self.ran = False

    def parse_args(self, argv):
        from argparse import Namespace
        out = argv[argv.index('-o') + 1] if '-o' in argv else '/nonexistent/db/ChatGPT'
        return Namespace(output_dir=out, dry_run='--dry-run' in argv, list=False)

    def run_session(self, page, args, sink=None):
        self.ran = True
        return self.code


@contextlib.contextmanager
def fake_browser(provider):
    with (patch('reclaim.core.service.provider_module', return_value=provider),
          patch('reclaim.core.service.browser.launch', return_value=(_Context(), object())),
          patch('playwright.sync_api.sync_playwright', return_value=_Playwright()),
          contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO())):
        yield


class TestRunChecks(Temp):
    def test_missing_custom_database_stops_before_the_browser(self):
        config.save(self.tmp, providers=['chatgpt'], archive=str(self.tmp / 'unmounted'))
        provider = _Provider()
        with fake_browser(provider):
            with self.assertRaisesRegex(database.DatabaseError, 'was not found'):
                ArchiveController(self.tmp).run(['chatgpt'])
        self.assertFalse(provider.ran)
        self.assertFalse((self.tmp / 'unmounted').exists())

    def test_default_database_is_created_by_an_update_but_not_by_a_preview(self):
        provider = _Provider()
        with fake_browser(provider):
            ArchiveController(self.tmp).run(['chatgpt'], 'dry-run')
            self.assertFalse((self.tmp / 'chats').exists())
            ArchiveController(self.tmp).run(['chatgpt'])
        self.assertTrue((self.tmp / 'chats' / database.MARKER).exists())
        self.assertIsNone(locks.read_holder(self.tmp / 'chats'))
        self.assertIsNone(locks.read_holder(self.tmp / '.playwright-profile'))

    def test_a_running_update_locks_out_a_second_one(self):
        database.init(self.tmp / 'chats')
        provider = _Provider()
        with fake_browser(provider), locks.hold(self.tmp / 'chats', 'update'):
            with self.assertRaises(locks.LockedError):
                ArchiveController(self.tmp).run(['chatgpt'])
        self.assertFalse(provider.ran)

    def test_invalid_settings_stop_the_window_and_command_line_runs(self):
        (self.tmp / '.reclaim.json').write_text('{"providers": ["chatgpt"],}')
        with fake_browser(_Provider()):
            with self.assertRaises(config.SettingsError):
                ArchiveController(self.tmp).run(['chatgpt'])

    def test_imports_check_and_lock_the_database(self):
        db = self.tmp / 'db'
        with self.assertRaises(database.DatabaseError):
            with offline_session(db, 'import chatgpt', root=self.tmp):
                pass
        database.init(db)
        with offline_session(db, 'import chatgpt', root=self.tmp):
            self.assertIsNotNone(locks.read_holder(db))
        self.assertIsNone(locks.read_holder(db))


class TestCommands(Temp):
    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with (patch.dict(os.environ, {'RECLAIM_HOME': str(self.tmp)}),
              contextlib.redirect_stdout(out), contextlib.redirect_stderr(err)):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_problems_print_a_message_and_a_next_step(self):
        config.save(self.tmp, providers=['chatgpt'], archive=str(self.tmp / 'unmounted'))
        code, _, err = self.run_cli('chatgpt', '--dry-run')
        self.assertEqual(code, 2)
        self.assertIn('error: The database folder', err)
        self.assertIn('reclaim init', err)
        code, out, _ = self.run_cli('status')
        self.assertEqual(code, 2)
        self.assertIn('was not found', out)

    def test_init_creates_or_marks_a_database(self):
        code, out, _ = self.run_cli('init')
        self.assertEqual(code, 0)
        self.assertIn('Created a new database', out)
        code, out, _ = self.run_cli('init')
        self.assertIn('already a Reclaim My Chats database', out)
        code, out, _ = self.run_cli('init', str(self.tmp / 'second'))
        self.assertIn('choose it as the database folder in Settings', out)

    def test_unlock_removes_only_locks_that_are_not_running(self):
        database.init(self.tmp / 'chats')
        (self.tmp / 'chats' / locks.LOCK_NAME).write_text(json.dumps(
            {'host': 'laptop-elsewhere', 'pid': 1, 'started': 'yesterday'}))
        mine = locks.acquire(self.tmp / '.playwright-profile', 'browser')
        code, out, _ = self.run_cli('unlock')
        self.assertEqual(code, 1)
        self.assertIn('removed the lock held by laptop-elsewhere', out)
        self.assertIn('still running', out)
        self.assertIsNotNone(locks.read_holder(self.tmp / '.playwright-profile'))
        code, _, _ = self.run_cli('unlock', '--force')
        self.assertEqual(code, 0)
        self.assertIsNone(locks.read_holder(self.tmp / '.playwright-profile'))
        locks.release(self.tmp / '.playwright-profile', mine)

    def test_invalid_settings_explain_the_problem(self):
        (self.tmp / '.reclaim.json').write_text('{"archive": 42}')
        code, _, err = self.run_cli('status')
        self.assertEqual(code, 2)
        self.assertIn('invalid "archive" value', err)


if __name__ == '__main__':
    unittest.main()
