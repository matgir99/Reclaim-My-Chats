"""Offline tests for Phase 4 chat lifecycle, moves, audit trail, and duplicate tidying."""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from reclaim import __main__ as cli
from reclaim import gui
from reclaim.core import autostart, browser, database, engine, lifecycle, retry, status, tidy, writer
from reclaim.core.manifest import SyncState, save_chat
from reclaim.core.model import Chat, Turn
from reclaim.core.writer import read_chat_json, write_chat, write_raw
from reclaim.providers import chatgpt, claude, deepseek, googleaistudio, googlegemini, kimi


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


class TestArchivedThenDeleted(unittest.TestCase):
    """A chat archived at the provider and later deleted there."""

    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.db = Path(self._temp.name) / 'chats'
        database.init(self.db)
        self.out = self.db / 'ChatGPT'
        self.out.mkdir()
        self.sync = SyncState(self.out, 'chatgpt')
        save_chat(_chat('c1', 'Active'), self.out, self.sync, 'c1', '1')
        for cid in ('a1', 'a2'):
            save_chat(_chat(cid, f'Archived {cid}'), self.out, self.sync, cid, '1', state='archived')
        self.asked: list[str] = []

    def tearDown(self):
        self._temp.cleanup()

    def _plan(self, archived_refs, verdicts: dict[str, str]) -> lifecycle.LifecyclePlan:
        def confirm(cid: str) -> str:
            self.asked.append(cid)
            return verdicts.get(cid, 'unconfirmed')
        return lifecycle.plan_lifecycle(
            self.out, 'chatgpt', [engine.ChatRef(id='c1', title='Active', updated_at='1')],
            self.sync, archived_refs=archived_refs, complete=True, confirm=confirm)

    def test_a_deleted_archived_chat_moves_from_archived_to_deleted(self):
        # a2 is still in the archived list; a1 is in neither list and the provider says 404.
        plan = self._plan([engine.ChatRef(id='a2', title='Archived a2', updated_at='1',
                                          state='archived')], {'a1': 'deleted'})
        self.assertEqual(self.asked, ['a1'])
        self.assertEqual([(m.chat_id, m.from_state, m.to_state) for m in plan.moves],
                         [('a1', 'archived', 'deleted')])
        lifecycle.apply_moves(self.out, self.sync, plan)
        self.assertTrue((self.out / '_Deleted' / 'Archived a1' / 'Archived a1.md').is_file())
        self.assertFalse((self.out / '_Archived' / 'Archived a1').exists())
        self.assertTrue((self.out / '_Archived' / 'Archived a2' / 'Archived a2.md').is_file())
        record = SyncState(self.out, 'chatgpt').known('a1')
        assert record is not None
        self.assertEqual(record['state'], 'deleted')

    def test_an_archived_chat_the_provider_still_has_stays_archived(self):
        plan = self._plan([], {'a1': 'archived', 'a2': 'unconfirmed'})
        self.assertEqual(sorted(self.asked), ['a1', 'a2'])
        self.assertEqual(plan.moves, [])
        self.assertEqual(sorted(plan.unconfirmed), ['a1', 'a2'])

    def test_without_an_archived_list_archived_chats_are_not_asked_about(self):
        # Archived chats are then missing from every listing, so absence says nothing.
        plan = self._plan(None, {'a1': 'deleted', 'a2': 'deleted'})
        self.assertEqual(self.asked, [])
        self.assertEqual(plan.moves, [])


class TestNestedChatFolders(unittest.TestCase):
    """A project named like a chat saves its chats inside that chat's folder.

    Moving or permanently deleting the outer chat must never take the chats
    inside it, or folders the owner added, along with it."""

    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.db = Path(self._temp.name) / 'chats'
        database.init(self.db)
        self.out = self.db / 'ChatGPT'
        self.out.mkdir()
        self.sync = SyncState(self.out, 'chatgpt')
        save_chat(_chat('outer', 'Foo', 'outer answer'), self.out, self.sync, 'outer', '1')
        save_chat(_chat('inner', 'Bar', 'inner answer', project='Foo'),
                  self.out, self.sync, 'inner', '1')

    def tearDown(self):
        self._temp.cleanup()

    def _entry(self, chat_id: str) -> lifecycle.StateChatEntry:
        return next(e for e in lifecycle.list_state_chats(self.db) if e.chat_id == chat_id)

    def test_the_layout_nests_the_project_chat_inside_the_chat_folder(self):
        # The layout the other tests protect; the writer produces it today.
        self.assertTrue((self.out / 'Foo' / 'chat.json').is_file())
        self.assertTrue((self.out / 'Foo' / 'Bar' / 'chat.json').is_file())

    def test_moving_a_chat_leaves_the_chats_inside_its_folder_in_place(self):
        target = lifecycle.move_chat_folder(self.out, self.sync, 'outer', 'deleted', reason='gone')
        self.assertEqual(target, self.out / '_Deleted' / 'Foo')
        payload = read_chat_json(self.out / '_Deleted' / 'Foo')
        assert payload is not None
        self.assertEqual(payload['state'], 'deleted')
        self.assertTrue((self.out / '_Deleted' / 'Foo' / 'Foo.md').is_file())
        self.assertFalse((self.out / 'Foo' / 'chat.json').exists())
        # The inner chat is untouched and its sync record still finds it.
        self.assertTrue((self.out / 'Foo' / 'Bar' / 'chat.json').is_file())
        record = SyncState(self.out, 'chatgpt').known('inner')
        assert record is not None
        self.assertEqual(record['state'], 'active')
        self.assertTrue(writer.locate_recorded(self.out, record['md'], 'inner').is_file())
        outer = SyncState(self.out, 'chatgpt').known('outer')
        assert outer is not None
        self.assertEqual(outer['state'], 'deleted')

    def test_restoring_a_chat_merges_into_a_folder_that_holds_other_chats(self):
        lifecycle.move_chat_folder(self.out, self.sync, 'outer', 'deleted', reason='gone')
        sync = SyncState(self.out, 'chatgpt')
        restored = lifecycle.move_chat_folder(self.out, sync, 'outer', 'active', reason='back')
        self.assertEqual(restored, self.out / 'Foo')
        self.assertTrue((self.out / 'Foo' / 'chat.json').is_file())
        self.assertTrue((self.out / 'Foo' / 'Foo.md').is_file())
        self.assertTrue((self.out / 'Foo' / 'Bar' / 'chat.json').is_file())
        self.assertFalse((self.out / '_Deleted').exists())

    def test_a_move_never_overwrites_a_file_that_is_already_there(self):
        lifecycle.move_chat_folder(self.out, self.sync, 'outer', 'deleted', reason='gone')
        (self.out / 'Foo' / 'Foo.md').write_text('the owner wrote this', encoding='utf-8')
        sync = SyncState(self.out, 'chatgpt')
        with self.assertRaisesRegex(FileExistsError, r'Foo\.md'):
            lifecycle.move_chat_folder(self.out, sync, 'outer', 'active', reason='back')
        self.assertEqual((self.out / 'Foo' / 'Foo.md').read_text(encoding='utf-8'),
                         'the owner wrote this')
        self.assertTrue((self.out / '_Deleted' / 'Foo' / 'chat.json').is_file())
        self.assertTrue((self.out / '_Deleted' / 'Foo' / 'Foo.md').is_file())

    def test_permanent_delete_keeps_the_chats_inside_the_folder(self):
        # Older versions moved the whole folder, leaving both chats under _Deleted.
        (self.out / '_Deleted').mkdir()
        (self.out / 'Foo').rename(self.out / '_Deleted' / 'Foo')
        self.assertEqual({e.chat_id for e in lifecycle.list_state_chats(self.db)},
                         {'outer', 'inner'})
        self.assertTrue(lifecycle.delete_state_chat_permanently(self.db, self._entry('outer')))
        self.assertFalse((self.out / '_Deleted' / 'Foo' / 'chat.json').exists())
        self.assertFalse((self.out / '_Deleted' / 'Foo' / 'Foo.md').exists())
        self.assertTrue((self.out / '_Deleted' / 'Foo' / 'Bar' / 'chat.json').is_file())
        self.assertTrue((self.out / '_Deleted' / 'Foo' / 'Bar' / 'Bar.md').is_file())
        # Deleting the inner chat afterwards leaves nothing behind.
        self.assertTrue(lifecycle.delete_state_chat_permanently(self.db, self._entry('inner')))
        self.assertFalse((self.out / '_Deleted').exists())

    def test_permanent_delete_keeps_folders_the_owner_added(self):
        lifecycle.move_chat_folder(self.out, self.sync, 'inner', 'deleted', reason='gone')
        notes = self.out / '_Deleted' / 'Foo' / 'Bar' / 'my notes'
        notes.mkdir()
        (notes / 'todo.txt').write_text('mine', encoding='utf-8')
        self.assertTrue(lifecycle.delete_state_chat_permanently(self.db, self._entry('inner')))
        self.assertEqual((notes / 'todo.txt').read_text(encoding='utf-8'), 'mine')
        self.assertFalse((self.out / '_Deleted' / 'Foo' / 'Bar' / 'chat.json').exists())

    def test_permanent_delete_of_a_plain_chat_removes_its_whole_folder(self):
        lifecycle.move_chat_folder(self.out, self.sync, 'inner', 'archived', reason='archived')
        folder = self.out / '_Archived' / 'Foo' / 'Bar'
        (folder / 'photo.png').write_bytes(b'png')  # a saved attachment
        self.assertTrue(lifecycle.delete_state_chat_permanently(self.db, self._entry('inner')))
        self.assertFalse((self.out / '_Archived').exists())
        self.assertTrue((self.out / 'Foo' / 'chat.json').is_file())  # the outer chat is untouched
        self.assertIsNone(SyncState(self.out, 'chatgpt').known('inner'))


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


class _Listed(engine.Provider):
    """A provider whose listing, archived listing and verdicts the test chooses."""
    name = 'chatgpt'
    folder = 'ChatGPT'
    pace = 0.0

    def __init__(self, refs, *, verdicts=None, archived=()):
        self._refs = list(refs)
        self._verdicts = verdicts or {}
        self._archived = list(archived)

    def open(self, page):
        return {'page': page, 'listing_complete': True}

    def listing(self, s):
        return list(self._refs)

    def archived_listing(self, s):
        return list(self._archived)

    def confirm_status(self, s, cid: str) -> str:
        return self._verdicts.get(cid, 'unconfirmed')

    def from_url(self, s, url):
        return engine.ChatRef(id=url.rsplit('/', 1)[-1])

    def fetch(self, s, ref):
        return engine.Fetched(_chat(ref.id, ref.title, project=ref.project), raw={'id': ref.id})


class _TempDatabase(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)
        self.db = self.root / 'chats'
        database.init(self.db)
        self.out = self.db / 'ChatGPT'
        self.out.mkdir()
        self.sync = SyncState(self.out, 'chatgpt')

    def tearDown(self):
        self._temp.cleanup()

    def update(self, provider, *args: str, replace_older: bool = False):
        """One run of `provider` with the given options and the setting on or off."""
        from reclaim.core import config  # local: keeps the shared import line untouched
        config.save(self.root, providers=['chatgpt'], archive=None, retire_older_copies=replace_older)
        events: list = []
        parsed = engine.parse_args(provider, [*args, '-o', str(self.out)])
        with config.use_root(self.root):
            code = engine.run_session(provider, None, parsed, sink=events.append)
        return code, events


class TestReplaceOlderCopies(_TempDatabase):
    """An update can replace older copies of a chat by the current one."""

    def duplicate(self, cid='dup', *, old_text='same answer', new_text='same answer', raw=None):
        """One chat saved in two folders; the sync record names the newer one."""
        old = write_chat(_chat(cid, 'Old Name', old_text, project='OldProj'), self.out, updated_at='1')
        new = write_chat(_chat(cid, 'New Name', new_text, project='NewProj'), self.out, updated_at='2')
        if raw is not None:
            write_raw(new['dir'], raw)
        self.sync.mark(cid, '2', str(new['md']), title='New Name', project='NewProj')
        return old['dir'], new['dir']

    def listed(self, cid='dup'):
        return _Listed([engine.ChatRef(cid, 'New Name', '2', project='NewProj')])

    def test_an_older_copy_is_replaced_and_the_audit_log_says_so(self):
        old, new = self.duplicate()
        replaced, kept = tidy.retire_older_copies(self.out, 'chatgpt')
        self.assertEqual(([c.stale_dir for c in replaced], kept), ([old], []))
        self.assertFalse(old.exists())
        self.assertTrue((new / 'chat.json').is_file())
        log = [json.loads(line)
               for line in (self.out / lifecycle.EVENTS_LOG).read_text().splitlines()]
        self.assertEqual([(e['chat_id'], e['state'], e['from'], e['to']) for e in log],
                         [('dup', 'replaced', 'OldProj/Old Name', 'NewProj/New Name')])

    def test_an_older_copy_with_text_the_current_copy_lacks_is_kept(self):
        old, _new = self.duplicate(old_text='only the older copy says this', new_text='another answer')
        replaced, kept = tidy.retire_older_copies(self.out, 'chatgpt')
        self.assertEqual((replaced, [c.stale_dir for c in kept]), ([], [old]))
        self.assertTrue(old.is_dir())
        self.assertFalse((self.out / lifecycle.EVENTS_LOG).exists())

    def test_text_found_only_in_the_current_raw_data_is_not_missing(self):
        # Older versions showed progress notes that the current version hides;
        # the current copy still holds them in raw.json.
        old, _new = self.duplicate(old_text='a progress note', new_text='the answer',
                                   raw={'message': {'parts': ['a progress note']}})
        replaced, kept = tidy.retire_older_copies(self.out, 'chatgpt')
        self.assertEqual((len(replaced), kept), (1, []))
        self.assertFalse(old.exists())

    def test_copies_filed_as_archived_or_deleted_are_never_replaced(self):
        current = write_chat(_chat('dup', 'Current', 'same answer'), self.out, updated_at='2')
        self.sync.mark('dup', '2', str(current['md']), title='Current')
        write_chat(_chat('dup', 'Filed', 'same answer'), self.out, updated_at='1', state='deleted')
        self.assertEqual(tidy.retire_older_copies(self.out, 'chatgpt'), ([], []))
        self.assertTrue((self.out / '_Deleted' / 'Filed').is_dir())

    def test_an_update_replaces_older_copies_only_when_the_setting_is_on(self):
        old, new = self.duplicate()
        code, events = self.update(self.listed(), replace_older=False)
        self.assertEqual(code, 0)
        self.assertTrue(old.is_dir())
        self.assertFalse([e for e in events if e.kind == 'replaced'])

        code, events = self.update(self.listed(), replace_older=True)
        self.assertEqual(code, 0)
        self.assertFalse(old.exists())
        self.assertTrue(new.is_dir())
        (event,) = [e for e in events if e.kind == 'replaced']
        self.assertEqual((event.provider, event.total), ('chatgpt', 1))
        self.assertEqual(event.message, 'replaced 1 older copy of chats that were saved in more than one folder')
        self.assertIn('New Name', event.detail)

    def test_a_run_that_did_not_see_every_chat_never_replaces(self):
        old, _new = self.duplicate()
        for options in (['--only-id', 'dup'], ['--limit', '1'], ['--skip', '0', '--limit', '5'],
                        ['New Name'], ['--dry-run']):
            with self.subTest(options):
                code, events = self.update(self.listed(), *options, replace_older=True)
                self.assertIn(code, (0, 1))
                self.assertTrue(old.is_dir())
                self.assertFalse([e for e in events if e.kind == 'replaced'])

    def test_older_copies_that_cannot_be_replaced_are_reported_not_hidden(self):
        old, _new = self.duplicate(old_text='only the older copy says this', new_text='another answer')
        _code, events = self.update(self.listed(), replace_older=True)
        self.assertTrue(old.is_dir())
        (warning,) = [e for e in events if e.kind == 'warning']
        self.assertIn('kept 1 older copy', warning.message)
        self.assertIn('Tidy duplicates', warning.message)


class TestUpdateReport(_TempDatabase):
    """A run says what it filed away or restored; the moves print nothing themselves."""

    def test_a_run_reports_the_chats_it_filed(self):
        for i in range(8):
            save_chat(_chat(f'c{i}', f'Chat {i}', project='P'), self.out, self.sync, f'c{i}', '1')
        provider = _Listed(
            [engine.ChatRef(f'c{i}', f'Chat {i}', '1', project='P') for i in (6, 7)],
            archived=[engine.ChatRef('c0', 'Chat 0', '1', state='archived')],
            verdicts={f'c{i}': 'deleted' for i in range(1, 6)})
        approve = [arg for i in range(6) for arg in ('--only-id', f'c{i}')]
        code, events = self.update(provider, *approve)
        self.assertEqual(code, 0)
        (moved,) = [e for e in events if e.kind == 'moved']
        self.assertEqual((moved.provider, moved.total), ('chatgpt', 6))
        self.assertEqual(moved.message, 'filed 6 chats (1 as archived, 5 as deleted)')
        self.assertIn('  - Chat 0 (archived)', moved.detail.splitlines())
        self.assertIn('  - Chat 3 (deleted)', moved.detail.splitlines())
        self.assertTrue((self.out / '_Deleted' / 'P' / 'Chat 3').is_dir())

    def test_a_run_reports_a_chat_that_came_back(self):
        save_chat(_chat('r1', 'Came back'), self.out, self.sync, 'r1', '1')
        lifecycle.move_chat_folder(self.out, self.sync, 'r1', 'deleted', reason='gone')
        provider = _Listed([engine.ChatRef('r1', 'Came back', '1')])
        _code, events = self.update(provider)
        (moved,) = [e for e in events if e.kind == 'moved']
        self.assertEqual(moved.message, 'restored 1 chat')
        self.assertIn('  - Came back (restored)', moved.detail.splitlines())

    def test_a_run_that_moved_nothing_says_nothing(self):
        save_chat(_chat('c1', 'Chat 1'), self.out, self.sync, 'c1', '1')
        _code, events = self.update(_Listed([engine.ChatRef('c1', 'Chat 1', '1')]))
        self.assertFalse([e for e in events if e.kind in ('moved', 'replaced')])

    def test_apply_moves_hands_back_the_moves_it_carried_out(self):
        for cid in ('a', 'b'):
            save_chat(_chat(cid, f'Chat {cid}'), self.out, self.sync, cid, '1')
        moves = [lifecycle.PlannedMove('a', 'Chat a', 'active', 'deleted', 'gone'),
                 lifecycle.PlannedMove('missing', 'No record', 'active', 'deleted', 'gone')]
        applied: list[lifecycle.PlannedMove] = []
        lifecycle.apply_moves(self.out, self.sync, lifecycle.LifecyclePlan('chatgpt', moves=moves),
                              applied=applied)
        self.assertEqual([m.chat_id for m in applied], ['a'])


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
        with patch.object(chatgpt, '_get', side_effect=retry.RequestFailed('HTTP 404', 404)):
            self.assertEqual(chatgpt.SPEC.confirm_status(sess, 'c1'), 'deleted')
        with patch.object(chatgpt, '_get', return_value={'is_archived': True}):
            self.assertEqual(chatgpt.SPEC.confirm_status(sess, 'c1'), 'archived')
        with patch.object(chatgpt, '_get', return_value={'mapping': {'n': {}}}):
            self.assertEqual(chatgpt.SPEC.confirm_status(sess, 'c1'), 'active')
        with patch.object(chatgpt, '_get', side_effect=RuntimeError('network timeout')):
            self.assertEqual(chatgpt.SPEC.confirm_status(sess, 'c1'), 'unconfirmed')


class _FakePage:
    """A page whose evaluate() answers with the given responses, one per request."""

    def __init__(self, *answers):
        self.answers = list(answers)

    def evaluate(self, *_args):
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def _answer(code: int, text: str = '') -> dict:
    return {'status': code, 'text': text or f'body {code}', 'retryAfter': None}


class TestDeletedMeansHttp404(unittest.TestCase):
    """A chat is called deleted only when the service answers HTTP 404.

    The text of an error can contain "404" for other reasons, such as the digits
    of a chat id (ChatGPT) or of an organisation id (Claude). These tests go
    through each provider's real request helper and the shared retry policy."""

    PROVIDERS = ('chatgpt', 'claude', 'deepseek', 'kimi')
    CHAT_ID = '6a404b00-0000-4000-8000-000000000404'  # contains 404 on purpose

    def confirm(self, name: str, *answers) -> str:
        page = _FakePage(*answers)
        spec, attributes = {
            'chatgpt': (chatgpt.SPEC, {'page': page, 'token': 'tok'}),
            'claude': (claude.SPEC, {'page': page, 'org': 'org-404'}),
            'deepseek': (deepseek.SPEC, {'page': page}),
            'kimi': (kimi.SPEC, {'page': page, 'token': 'tok'}),
        }[name]
        session = type('S', (), attributes)()
        with (patch.object(retry.time, 'sleep'),
              patch.object(kimi, 'base_url', return_value='https://kimi.example')):
            return spec.confirm_status(session, self.CHAT_ID)

    def test_an_http_404_means_deleted(self):
        for name in self.PROVIDERS:
            with self.subTest(name):
                self.assertEqual(self.confirm(name, _answer(404)), 'deleted')

    def test_any_other_failure_leaves_the_chat_unconfirmed_even_if_the_text_says_404(self):
        said_404 = 'upstream said 404 not found'
        failures = {
            'a server error': [_answer(500, said_404)] * 3,
            'a rate limit': [_answer(429)] * 6,
            'a refused login': [_answer(403, said_404)] * 3,
            'a dropped connection': [RuntimeError(said_404)] * 3,
        }
        for name in self.PROVIDERS:
            for label, answers in failures.items():
                with self.subTest(name, failure=label):
                    self.assertEqual(self.confirm(name, *answers), 'unconfirmed')


class TestAllProviderConfirmAndSubsetAndAutostart(unittest.TestCase):
    def test_confirm_status_for_kimi_gemini_and_aistudio(self):
        ks = type('S', (), {'page': object(), 'token': 'tok'})()
        with patch.object(kimi, '_api', return_value={'chat': {'id': 'k1'}}):
            self.assertEqual(kimi.SPEC.confirm_status(ks, 'k1'), 'active')
        with patch.object(kimi, '_api', side_effect=retry.RequestFailed('HTTP 404 not found', 404)):
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

    def test_start_at_login_quotes_a_path_with_spaces_the_way_each_system_reads_it(self):
        # The Windows installer's default location has spaces; Windows reads only
        # double quotes, as in the installer's own entry ("{app}\ReclaimMyChats.exe").
        exe = r'C:\Users\A User\AppData\Local\Programs\Reclaim My Chats\ReclaimMyChats.exe'
        written: list[str] = []
        with patch.object(autostart.sys, 'executable', exe), \
                patch.object(autostart.sys, 'frozen', True, create=True):
            autostart.set_autostart(True, system='win32',
                                    win_setter=lambda on, cmd: written.append(cmd))
        self.assertEqual(written, [f'"{exe}"'])
        python = r'C:\Program Files\Python312\python.exe'
        with patch.object(autostart.sys, 'executable', python):
            self.assertEqual(autostart.default_command('win32'), f'"{python}" -m reclaim gui')

        app = '/Applications/Reclaim My Chats.app/Contents/MacOS/ReclaimMyChats'
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(autostart.sys, 'executable', app), \
                patch.object(autostart.sys, 'frozen', True, create=True):
            plist = autostart.set_autostart(True, home=Path(tmp), system='darwin')
            assert isinstance(plist, Path)
            self.assertIn(f'<string>{app}</string>', plist.read_text(encoding='utf-8'))
            self.assertEqual(autostart.default_command('linux'), f"'{app}'")

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

        headless_env = {
            k: v for k, v in os.environ.items()
            if k not in ('DISPLAY', 'WAYLAND_DISPLAY', 'DBUS_SESSION_BUS_ADDRESS')
        }
        headless_env['RECLAIM_NO_NOTIFY'] = ''
        with (patch.dict(os.environ, headless_env, clear=True),
              patch.object(autostart.shutil, 'which', return_value='/usr/bin/notify-send'),
              patch.object(autostart.subprocess, 'run') as mock_run):
            self.assertFalse(autostart.notify_desktop('Reclaim My Chats', 'x', system='linux'))
            mock_run.assert_not_called()

        with (patch.dict(os.environ, {**headless_env, 'DISPLAY': ':0'}, clear=True),
              patch.object(autostart.shutil, 'which', return_value='/usr/bin/notify-send'),
              patch.object(autostart.subprocess, 'run') as mock_run):
            mock_run.return_value = type('R', (), {'returncode': 0})()
            self.assertTrue(autostart.notify_desktop('Reclaim My Chats', 'ok', system='linux'))
            mock_run.assert_called_once_with(
                ['notify-send', 'Reclaim My Chats', 'ok'],
                stdout=autostart.subprocess.DEVNULL,
                stderr=autostart.subprocess.DEVNULL,
                check=False,
                timeout=2,
            )
            mock_run.return_value = type('R', (), {'returncode': 1})()
            self.assertFalse(autostart.notify_desktop('Reclaim My Chats', 'fail', system='linux'))

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

    def test_state_folders_dry_run_renames_empty_listing_and_approved_brake(self):
        class _FakeStateProv(engine.Provider):
            name = 'chatgpt'
            folder = 'ChatGPT'
            pace = 0.0

            def __init__(self, active_refs, verdicts):
                self._active = active_refs
                self._verdicts = verdicts
                self.fetched: list[str] = []

            def open(self, page):
                return {'page': page, 'listing_complete': True}

            def listing(self, s):
                return list(self._active)

            def confirm_status(self, s, cid: str) -> str:
                return self._verdicts.get(cid, 'unconfirmed')

            def from_url(self, s, url):
                return engine.ChatRef(id=url.rsplit('/', 1)[-1])

            def fetch(self, s, ref):
                self.fetched.append(ref.id)
                return engine.Fetched(_chat(ref.id, ref.title, project=ref.project), raw={'id': ref.id})

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / 'chats'
            database.init(db)
            out = db / 'ChatGPT'
            out.mkdir()
            sync = SyncState(out, 'chatgpt')
            for i in range(5):
                save_chat(_chat(f'c{i}', f'Chat {i}', project='OldProj'), out, sync, f'c{i}', '1')
            # Brand new archived chat lands under _Archived/ when written directly.
            save_chat(_chat('arc0', 'Pre Archived', project='Work'), out, sync, 'arc0', '1',
                      state='archived')
            self.assertTrue((out / '_Archived' / 'Work' / 'Pre Archived').is_dir())

            # Renamed chat with unchanged timestamp shows in dry-run + --only-id confirms braked move.
            prov = _FakeStateProv(
                active_refs=[engine.ChatRef('c0', 'Renamed Zero', '1', project='NewProj')],
                verdicts={f'c{i}': 'deleted' for i in range(1, 5)},
            )
            events = []
            dry = engine.parse_args(prov, ['--dry-run', '-o', str(out)])
            self.assertEqual(engine.run_session(prov, None, dry, sink=events.append), 0)
            self.assertIn(('c0', 'fetch (changed)'),
                          [(e.detail, e.message) for e in events if e.kind == 'chat_planned'])
            self.assertTrue(any(e.kind == 'plan' and 'would move:' in e.message for e in events))

            approved = engine.parse_args(prov, ['--only-id', 'c0', '--only-id', 'c1', '-o', str(out)])
            self.assertEqual(engine.run_session(prov, None, approved), 0)
            self.assertTrue((out / '_Deleted' / 'OldProj' / 'Chat 1').is_dir())
            self.assertTrue((out / 'NewProj' / 'Renamed Zero').is_dir())
            self.assertTrue((out / 'OldProj' / 'Chat 2').is_dir())  # unapproved stays put

            # Even when the server listing is completely empty, lifecycle runs instead of failing early.
            empty_prov = _FakeStateProv(active_refs=[], verdicts={'c2': 'deleted', 'c3': 'deleted'})
            empty_events = []
            empty_dry = engine.parse_args(empty_prov, ['--dry-run', '-o', str(out)])
            self.assertEqual(engine.run_session(empty_prov, None, empty_dry, sink=empty_events.append), 0)
            self.assertTrue(any(e.kind == 'chat_planned' and e.detail == 'c2' for e in empty_events))

    def test_lifecycle_reconciliation_stops_on_cancellation(self):
        stop = threading.Event()
        confirmed: list[str] = []

        class _CancelProv(engine.Provider):
            name = 'chatgpt'
            folder = 'ChatGPT'
            pace = 0.0

            def __init__(self, stop_in_archived: bool = False):
                self._stop_in_archived = stop_in_archived
                self.fetched: list[str] = []

            def open(self, page):
                return {'page': page, 'listing_complete': True}

            def listing(self, s):
                return [engine.ChatRef('keep', 'Keep', '1'), engine.ChatRef('new1', 'New One', '1')]

            def archived_listing(self, s):
                if self._stop_in_archived:
                    stop.set()
                    return [engine.ChatRef('a0', 'Arch Zero', '1', state='archived')]
                return []

            def confirm_status(self, s, cid: str) -> str:
                confirmed.append(cid)
                stop.set()
                return 'deleted'

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
            for cid, title in (('keep', 'Keep'), ('a0', 'Arch Zero'),
                               ('d1', 'Gone 1'), ('d2', 'Gone 2'), ('d3', 'Gone 3')):
                save_chat(_chat(cid, title), out, sync, cid, '1')

            # 1) Stop pressed during confirm_status skips remaining confirmations, moves & download.
            prov1 = _CancelProv(stop_in_archived=False)
            events1 = []
            args1 = engine.parse_args(prov1, ['-o', str(out)])
            with engine.stoppable(stop):
                self.assertEqual(engine.run_session(prov1, None, args1, sink=events1.append), 0)
            self.assertEqual(len(confirmed), 1)
            self.assertEqual(prov1.fetched, [])
            self.assertFalse((out / '_Deleted').exists())
            self.assertTrue(any(e.kind == 'stopped' for e in events1))

            # 2) Stop pressed during archived_listing skips confirm_status, moves & download.
            stop.clear()
            confirmed.clear()
            prov2 = _CancelProv(stop_in_archived=True)
            events2 = []
            args2 = engine.parse_args(prov2, ['-o', str(out)])
            with engine.stoppable(stop):
                self.assertEqual(engine.run_session(prov2, None, args2, sink=events2.append), 0)
            self.assertEqual(confirmed, [])
            self.assertEqual(prov2.fetched, [])
            self.assertFalse((out / '_Archived').exists())
            self.assertTrue(any(e.kind == 'stopped' for e in events2))
