"""Offline tests for Phase 4 chat lifecycle, moves, audit trail, and duplicate tidying."""

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
from reclaim import gui
from reclaim.core import autostart, browser, database, engine, lifecycle, status, tidy
from reclaim.core.manifest import SyncState, save_chat
from reclaim.core.model import Chat, Turn
from reclaim.core.writer import read_chat_json, write_chat, write_raw
from reclaim.providers import chatgpt, googleaistudio, googlegemini, kimi


def _chat(cid: str, title: str, text: str = 'hello', *, project: str | None = None) -> Chat:
    return Chat(
        id=cid, title=title, source_url=f'https://example.test/{cid}',
        turns=[Turn('user', 'q'), Turn('model', text)],
        provider='chatgpt', project=project,
    )


class TestLifecycleAndSyncRecords(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)
        self.db = self.root / 'chats'
        database.init(self.db)
        self.out = self.db / 'ChatGPT'
        self.out.mkdir()

    def tearDown(self):
        self._temp.cleanup()

    def test_sync_records_track_title_project_and_state_and_detect_moves(self):
        sync = SyncState(self.out, 'chatgpt')
        save_chat(_chat('c1', 'Alpha', project='Work'), self.out, sync, 'c1', 't1')
        rec = SyncState(self.out, 'chatgpt').known('c1')
        assert rec is not None
        self.assertEqual(rec['title'], 'Alpha')
        self.assertEqual(rec['project'], 'Work')
        self.assertEqual(rec['state'], 'active')
        self.assertEqual(sync.classify('c1', 't1', title='Alpha Renamed', project='Work'), 'changed')
        self.assertEqual(sync.classify('c1', 't1', title='Alpha', project='Personal'), 'changed')
        self.assertEqual(sync.classify('c1', 't1', title='Alpha', project='Work'), 'unchanged')

        # Rebuild preserves title, project, and state inside _Archived
        moved = lifecycle.move_chat_folder(self.out, sync, 'c1', 'archived', reason='archived at provider')
        assert moved is not None
        self.assertTrue((self.out / '_Archived' / 'Work' / 'Alpha' / 'Alpha.md').exists())
        self.assertEqual((read_chat_json(moved) or {}).get('state'), 'archived')
        (self.out / '.last_sync_chatgpt.json').unlink()
        rebuilt = SyncState(self.out, 'chatgpt').known('c1')
        assert rebuilt is not None
        self.assertEqual(rebuilt['state'], 'archived')
        self.assertEqual(rebuilt['project'], 'Work')

    def test_move_to_deleted_restore_and_audit_trail_with_status_counts(self):
        sync = SyncState(self.out, 'chatgpt')
        save_chat(_chat('a1', 'Keep'), self.out, sync, 'a1', '1')
        save_chat(_chat('d1', 'Gone', project='Work'), self.out, sync, 'd1', '1')

        deleted = lifecycle.move_chat_folder(self.out, sync, 'd1', 'deleted',
                                             reason='confirmed missing')
        assert deleted is not None
        self.assertTrue((self.out / '_Deleted' / 'Work' / 'Gone' / 'Gone.md').exists())
        self.assertFalse((self.out / 'Work').exists())
        events = [json.loads(line) for line in (self.out / lifecycle.EVENTS_LOG).read_text().splitlines()]
        self.assertEqual(events[0]['state'], 'deleted')
        self.assertEqual(events[0]['from'], 'Work/Gone')
        self.assertEqual(events[0]['to'], '_Deleted/Work/Gone')

        info = next(i for i in status.scan(self.db) if i.provider == 'chatgpt')
        self.assertEqual((info.chats, info.archived_chats, info.deleted_chats), (1, 0, 1))
        self.assertIn('states: 1 active · 0 archived · 1 deleted', status.format_status(info))

        restored = lifecycle.move_chat_folder(self.out, sync, 'd1', 'active', reason='restored')
        assert restored is not None
        self.assertTrue((self.out / 'Work' / 'Gone' / 'Gone.md').exists())
        self.assertEqual((read_chat_json(restored) or {}).get('state'), 'active')

    def test_planner_completeness_confirmation_and_mass_change_brake(self):
        sync = SyncState(self.out, 'chatgpt')
        for i in range(6):
            save_chat(_chat(f'c{i}', f'Chat {i}'), self.out, sync, f'c{i}', '1')

        # Incomplete listing never proposes deletions
        incomplete = lifecycle.plan_lifecycle(
            self.out, 'chatgpt', [engine.ChatRef(id='c0', title='Chat 0', updated_at='1')],
            sync, complete=False, confirm=lambda _cid: 'deleted',
        )
        self.assertEqual(incomplete.moves, [])
        self.assertEqual(lifecycle.apply_moves(self.out, sync, incomplete), [])

        # Confirming 2 of 6 (>10% of 6) triggers the mass-change brake
        verdicts = {'c1': 'deleted', 'c2': 'archived', 'c3': 'network-error'}
        braked = lifecycle.plan_lifecycle(
            self.out, 'chatgpt', [engine.ChatRef(id=f'c{i}', title=f'Chat {i}', updated_at='1')
                                  for i in (0, 4, 5)],
            sync, complete=True, confirm=lambda cid: verdicts.get(cid, 'active'),
        )
        self.assertTrue(braked.brake_triggered)
        self.assertIn('c3', braked.unconfirmed)
        self.assertEqual(lifecycle.apply_moves(self.out, sync, braked), [])
        moved = lifecycle.apply_moves(self.out, sync, braked, approved_ids={'c1'})
        self.assertEqual(len(moved), 1)
        self.assertTrue((self.out / '_Deleted' / 'Chat 1').exists())
        self.assertTrue((self.out / 'Chat 2').exists())


class TestTidyDuplicates(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)
        self.db = self.root / 'chats'
        database.init(self.db)
        self.out = self.db / 'ChatGPT'
        self.out.mkdir()

    def tearDown(self):
        self._temp.cleanup()

    def test_find_and_retire_duplicates_keeps_unique_branch_and_runs_cli(self):
        # Safe duplicate: old and new hold identical text
        old_safe = write_chat(_chat('s1', 'Safe Old', 'same answer'), self.out, updated_at='1')
        new_safe = write_chat(_chat('s1', 'Safe New', 'same answer'), self.out, updated_at='2')
        os.utime(old_safe['dir'] / 'chat.json', (1, 1))

        # Unique duplicate: old has an extra answer missing from new's chat.json and raw.json
        old_uniq = write_chat(_chat('u1', 'Unique Old', 'exclusive branch'), self.out, updated_at='1')
        new_uniq = write_chat(_chat('u1', 'Unique New', 'newer branch'), self.out, updated_at='2')
        write_raw(new_uniq['dir'], {'text': 'unrelated'})
        os.utime(old_uniq['dir'] / 'chat.json', (1, 1))

        sync = SyncState(self.out, 'chatgpt')
        sync.mark('s1', '2', str(new_safe['md']), title='Safe New')
        sync.mark('u1', '2', str(new_uniq['md']), title='Unique New')

        copies = tidy.find_duplicates(self.db)
        self.assertEqual(len(copies), 2)
        by_id = {c.chat_id: c for c in copies}
        self.assertFalse(by_id['s1'].has_unique_turns)
        self.assertTrue(by_id['u1'].has_unique_turns)

        buf = io.StringIO()
        with (patch.dict(os.environ, {'RECLAIM_HOME': str(self.root)}),
              contextlib.redirect_stdout(buf)):
            self.assertEqual(cli.main(['tidy']), 0)
            self.assertTrue(old_safe['dir'].exists())
            self.assertEqual(cli.main(['tidy', '--apply']), 0)
        self.assertFalse(old_safe['dir'].exists())
        self.assertTrue(old_uniq['dir'].exists())
        self.assertIn('Retired 1 stale duplicate folder', buf.getvalue())


@unittest.skipUnless(gui.ft is not None, 'Flet GUI extra is not installed')
class TestGuiTidy(unittest.IsolatedAsyncioTestCase):
    async def test_tidy_dialog_previews_and_retires_duplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / 'chats'
            database.init(db)
            out = db / 'ChatGPT'
            old = write_chat(_chat('dup1', 'Old Copy', 'same'), out, updated_at='1')
            new = write_chat(_chat('dup1', 'New Copy', 'same'), out, updated_at='2')
            os.utime(old['dir'] / 'chat.json', (1, 1))
            SyncState(out, 'chatgpt').mark('dup1', '2', str(new['md']), title='New Copy')

            class _Page:
                def __init__(self):
                    self.window = type('W', (), {})()
                    self.dialog = None

                def add(self, *_):
                    pass

                def update(self, *_):
                    pass

                def show_dialog(self, d):
                    self.dialog = d

                def pop_dialog(self):
                    self.dialog = None

            page = _Page()
            window = gui.ReclaimWindow(page, root)
            await window.refresh_archive()
            await window._tidy_dialog()
            dialog = page.dialog
            assert dialog is not None and dialog.actions is not None
            await dialog.actions[1].on_click(None)
            self.assertFalse(old['dir'].exists())
            self.assertIn('Retired 1 stale duplicate', window.summary.value)


class TestChatGPTLifecycleSignals(unittest.TestCase):
    def test_list_archived_and_confirm_status(self):
        with patch.object(chatgpt, '_get', side_effect=[
            {'items': [{'id': 'arch1', 'title': 'Old archived', 'update_time': 10}]},
            {'items': []},
        ]) as getter:
            archived = chatgpt.list_archived_conversations(object(), 'tok')
        self.assertEqual([a['id'] for a in archived], ['arch1'])
        self.assertIn('is_archived=true', getter.call_args_list[0].args[2])

        sess = type('S', (), {'page': object(), 'token': 'tok'})()
        with patch.object(chatgpt, '_get', side_effect=RuntimeError('HTTP 404')):
            self.assertEqual(chatgpt.SPEC.confirm_status(sess, 'c1'), 'deleted')
        with patch.object(chatgpt, '_get', return_value={'is_archived': True}):
            self.assertEqual(chatgpt.SPEC.confirm_status(sess, 'c1'), 'archived')
        with patch.object(chatgpt, '_get', return_value={'mapping': {'n': {}}}):
            self.assertEqual(chatgpt.SPEC.confirm_status(sess, 'c1'), 'active')
        with patch.object(chatgpt, '_get', side_effect=RuntimeError('network timeout')):
            self.assertEqual(chatgpt.SPEC.confirm_status(sess, 'c1'), 'unconfirmed')


class TestAllProviderConfirmAndSubsetAndAutostart(unittest.TestCase):
    def test_confirm_status_for_kimi_gemini_and_aistudio(self):
        ks = type('S', (), {'page': object(), 'token': 'tok'})()
        with patch.object(kimi, '_api', return_value={'chat': {'id': 'k1'}}):
            self.assertEqual(kimi.SPEC.confirm_status(ks, 'k1'), 'active')
        with patch.object(kimi, '_api', side_effect=RuntimeError('HTTP 404 not found')):
            self.assertEqual(kimi.SPEC.confirm_status(ks, 'k1'), 'deleted')

        gs = type('S', (), {'page': object(), 'tokens': {}, 'reqid': 1})()
        with patch.object(googlegemini, 'rpc_call', return_value=(404, '')):
            self.assertEqual(googlegemini.SPEC.confirm_status(gs, 'g1'), 'deleted')
        with (patch.object(googlegemini, 'rpc_call', return_value=(200, 'raw')),
              patch.object(googlegemini, 'decode_response', return_value=[])):
            self.assertEqual(googlegemini.SPEC.confirm_status(gs, 'g1'), 'deleted')
        with (patch.object(googlegemini, 'rpc_call', return_value=(200, 'raw')),
              patch.object(googlegemini, 'decode_response', return_value=['nonempty']),
              patch.object(googlegemini, 'parse_read', return_value=[{'role': 'user'}])):
            self.assertEqual(googlegemini.SPEC.confirm_status(gs, 'g1'), 'active')

        ais = type('S', (), {'page': object()})()
        with patch.object(googleaistudio, 'rpc_call', return_value=(404, '')):
            self.assertEqual(googleaistudio.SPEC.confirm_status(ais, 'a1'), 'deleted')
        with (patch.object(googleaistudio, 'rpc_call', return_value=(200, '{}')),
              patch.object(googleaistudio, 'parse_rpc', return_value={'entries': [{'text': 'hi'}]})):
            self.assertEqual(googleaistudio.SPEC.confirm_status(ais, 'a1'), 'active')

    def test_no_login_wait_and_only_ids_subset_download(self):
        with (browser.no_login_wait(),
              patch('reclaim.core.browser.emit') as emitted,
              self.assertRaises(browser.LoginRequiredError)):
            browser.wait_for_login(object(), 'chatgpt', 'ChatGPT', lambda: None)
        emitted.assert_called_once()

        class _Prov(engine.Provider):
            name = 'chatgpt'
            folder = 'ChatGPT'
            pace = 0.0

            def fetch(self, session, ref):
                return engine.Fetched(_chat(ref.id, ref.title))

        prov = _Prov()
        args = engine.parse_args(prov, ['--only-id', 'b'])
        self.assertEqual(args.only_ids, ['b'])
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / 'ChatGPT'
            out.mkdir()
            results = engine.download(
                prov, None,
                [engine.ChatRef('a', 'Alpha'), engine.ChatRef('b', 'Beta')],
                out, only_ids=set(args.only_ids),
            )
            self.assertEqual([r['id'] for r in results], ['b'])
            self.assertTrue((out / 'Beta').exists())
            self.assertFalse((out / 'Alpha').exists())

    def test_list_restore_and_permanent_delete_in_state_folders(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / 'chats'
            database.init(db)
            pdir = db / 'ChatGPT'
            pdir.mkdir()
            sync = SyncState(pdir, 'chatgpt')
            save_chat(_chat('act', 'Active Keep'), pdir, sync, 'act', '1')
            save_chat(_chat('arc', 'Archived Chat', project='Work'), pdir, sync, 'arc', '1')
            lifecycle.move_chat_folder(pdir, sync, 'arc', 'archived', reason='archived')

            entries = lifecycle.list_state_chats(db)
            self.assertEqual([(e.chat_id, e.state) for e in entries], [('arc', 'archived')])

            # Active folder cannot be permanently deleted via delete_state_chat_permanently
            active_entry = lifecycle.StateChatEntry(
                'chatgpt', 'act', 'Active Keep', 'active',
                pdir / 'Active Keep', pdir / 'Active Keep' / 'Active Keep.md')
            self.assertFalse(lifecycle.delete_state_chat_permanently(db, active_entry))
            self.assertTrue((pdir / 'Active Keep').exists())

            # Restore and re-archive then permanently delete
            restored = lifecycle.restore_state_chat(db, entries[0])
            assert restored is not None
            self.assertTrue((pdir / 'Work' / 'Archived Chat').exists())

            sync_now = SyncState(pdir, 'chatgpt')
            lifecycle.move_chat_folder(pdir, sync_now, 'arc', 'deleted', reason='deleted')
            del_entries = lifecycle.list_state_chats(db)
            self.assertTrue(lifecycle.delete_state_chat_permanently(db, del_entries[0]))
            self.assertFalse((pdir / '_Deleted').exists())
            self.assertIsNone(SyncState(pdir, 'chatgpt').known('arc'))

    def test_autostart_cross_platform_and_desktop_notification(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            linux_path = autostart.set_autostart(True, command='reclaim-gui', home=home, system='linux')
            assert isinstance(linux_path, Path) and linux_path.is_file()
            self.assertIn('Exec=reclaim-gui', linux_path.read_text(encoding='utf-8'))
            autostart.set_autostart(False, home=home, system='linux')
            self.assertFalse(linux_path.exists())

            mac_path = autostart.set_autostart(True, command='reclaim-gui', home=home, system='darwin')
            assert isinstance(mac_path, Path) and mac_path.is_file()
            self.assertIn('<string>reclaim-gui</string>', mac_path.read_text(encoding='utf-8'))
            autostart.set_autostart(False, home=home, system='darwin')
            self.assertFalse(mac_path.exists())

            win_calls: list[tuple[bool, str]] = []
            autostart.set_autostart(True, command='ReclaimMyChats.exe', system='win32',
                                    win_setter=lambda on, cmd: win_calls.append((on, cmd)))
            self.assertEqual(win_calls, [(True, 'ReclaimMyChats.exe')])

        seen_cmds: list[list[str]] = []
        with patch.dict(os.environ, {'RECLAIM_NO_NOTIFY': ''}):
            self.assertTrue(autostart.notify_desktop(
                'Reclaim My Chats', 'Update completed', runner=seen_cmds.append, system='linux'))
            self.assertEqual(seen_cmds, [['notify-send', 'Reclaim My Chats', 'Update completed']])
        with patch.dict(os.environ, {'RECLAIM_NO_NOTIFY': '1'}):
            self.assertFalse(autostart.notify_desktop('Reclaim My Chats', 'x', runner=seen_cmds.append))

    def test_engine_session_applies_lifecycle_moves_and_honors_brake(self):
        class _FakeLifecycleProvider(engine.Provider):
            name = 'chatgpt'
            folder = 'ChatGPT'
            pace = 0.0

            def __init__(self, active_refs, archived_refs, verdicts):
                self._active = active_refs
                self._archived = archived_refs
                self._verdicts = verdicts
                self.fetched: list[str] = []

            def open(self, page):
                return {'page': page, 'listing_complete': True}

            def listing(self, s):
                return list(self._active)

            def archived_listing(self, s):
                return list(self._archived)

            def confirm_status(self, s, cid: str) -> str:
                return self._verdicts.get(cid, 'unconfirmed')

            def from_url(self, s, url):
                return engine.ChatRef(id=url.rsplit('/', 1)[-1])

            def fetch(self, s, ref):
                self.fetched.append(ref.id)
                return engine.Fetched(_chat(ref.id, ref.title), raw={'id': ref.id})

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / 'chats'
            database.init(db)
            out = db / 'ChatGPT'
            out.mkdir()
            sync = SyncState(out, 'chatgpt')
            for cid, title in (('keep', 'Keep'), ('unknown', 'Keep Absent'),
                               ('arc', 'To Archive'), ('gone', 'To Delete')):
                save_chat(_chat(cid, title), out, sync, cid, '1')

            prov = _FakeLifecycleProvider(
                active_refs=[engine.ChatRef('keep', 'Keep', '1'),
                             engine.ChatRef('new_chat', 'Brand New', '1')],
                archived_refs=[engine.ChatRef('arc', 'To Archive', '1', state='archived')],
                verdicts={'gone': 'deleted', 'unknown': 'unconfirmed'},
            )
            dry_events = []
            dry_args = engine.parse_args(prov, ['--dry-run', '-o', str(out)])
            self.assertEqual(engine.run_session(prov, None, dry_args, sink=dry_events.append), 0)
            planned = [(e.detail, e.message) for e in dry_events if e.kind == 'chat_planned']
            self.assertIn(('arc', 'move (archived)'), planned)
            self.assertIn(('gone', 'move (deleted)'), planned)
            self.assertFalse((out / '_Archived').exists())
            self.assertFalse((out / '_Deleted').exists())

            up_args = engine.parse_args(prov, ['-o', str(out)])
            self.assertEqual(engine.run_session(prov, None, up_args), 0)
            self.assertEqual(prov.fetched, ['new_chat'])
            self.assertTrue((out / '_Archived' / 'To Archive').is_dir())
            self.assertTrue((out / '_Deleted' / 'To Delete').is_dir())
            self.assertTrue((out / 'Keep').is_dir())
            self.assertTrue((out / 'Keep Absent').is_dir())
            self.assertTrue((out / 'Brand New').is_dir())

            braked_out = db / 'ChatGPTBraked'
            braked_out.mkdir()
            sync2 = SyncState(braked_out, 'chatgpt')
            for i in range(6):
                save_chat(_chat(f'c{i}', f'Chat {i}'), braked_out, sync2, f'c{i}', '1')
            prov2 = _FakeLifecycleProvider(
                active_refs=[engine.ChatRef('c0', 'Chat 0', '1')],
                archived_refs=[],
                verdicts={f'c{i}': 'deleted' for i in range(1, 6)},
            )
            brake_events = []
            args2 = engine.parse_args(prov2, ['-o', str(braked_out)])
            self.assertEqual(engine.run_session(prov2, None, args2, sink=brake_events.append), 0)
            self.assertTrue(any(e.kind == 'warning' and 'Mass-change brake' in e.message
                                for e in brake_events))
            self.assertFalse((braked_out / '_Deleted').exists())
