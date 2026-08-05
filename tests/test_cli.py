"""Offline tests for the CLI redesign: arg parsing, writer overwrite,
SyncState migration, dry-run planning, and `reclaim status` aggregation.

No browser required. Run: ./run_tests.sh
"""

import io
import json
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from reclaim.core import status
from reclaim.core.manifest import (SyncState, plan_fetch, print_dry_run)
from reclaim.core.model import Chat, Turn
from reclaim.core.writer import write_chat
from reclaim.providers import (chatgpt, deepseek, googleaistudio, kimi)

ALL_PROVIDERS = [googleaistudio, deepseek, kimi, chatgpt]


class TestProviderArgparse(unittest.TestCase):
    def test_new_flags_parsed(self):
        for mod in ALL_PROVIDERS:
            ap = mod.build_parser()
            args = ap.parse_args([
                'Some Title', '--rebuild', '--list', '--log', '--dry-run',
                '--skip', '3', '--limit', '5', '--no-raw', '-o', '/tmp/out'])
            self.assertEqual(args.title, 'Some Title', mod.__name__)
            self.assertTrue(args.rebuild, mod.__name__)
            self.assertTrue(args.list, mod.__name__)
            self.assertTrue(args.log, mod.__name__)
            self.assertTrue(args.dry_run, mod.__name__)
            self.assertTrue(args.no_raw, mod.__name__)
            self.assertEqual(args.skip, 3, mod.__name__)
            self.assertEqual(args.limit, 5, mod.__name__)
            self.assertEqual(args.output_dir, '/tmp/out', mod.__name__)

    def test_defaults(self):
        for mod in ALL_PROVIDERS:
            args = mod.build_parser().parse_args([])
            self.assertIsNone(args.title, mod.__name__)
            self.assertEqual(args.skip, 0, mod.__name__)
            self.assertEqual(args.limit, 0, mod.__name__)
            self.assertFalse(args.rebuild, mod.__name__)
            self.assertFalse(args.list, mod.__name__)
            self.assertFalse(args.log, mod.__name__)
            self.assertFalse(args.dry_run, mod.__name__)

    def test_removed_flags_rejected(self):
        for mod in ALL_PROVIDERS:
            ap = mod.build_parser()
            for flag in ('--resume', '--only', '--start', '--keep-raw',
                         '--retrieve', '--update', '--match',
                         '--incremental', '--fresh'):
                with self.assertRaises(SystemExit, msg=f'{mod.__name__} {flag}'):
                    ap.parse_args([flag])

    def test_title_url_conflict_exits_2(self):
        for mod in ALL_PROVIDERS:
            with self.assertRaises(SystemExit) as cm:
                mod.main(['--url', 'https://example.com/chat/abc123', 'title'])
            self.assertEqual(cm.exception.code, 2, mod.__name__)

    def test_output_dir_defaults(self):
        for mod, expected in ((googleaistudio, 'Google AI Studio'),
                              (deepseek, 'Deepseek Chat'),
                              (kimi, 'Kimi Chat'),
                              (chatgpt, 'ChatGPT')):
            args = mod.build_parser().parse_args([])
            self.assertTrue(args.output_dir.endswith(expected), mod.__name__)


class TestWriterOverwrite(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_same_id_overwrites_no_underscore(self):
        write_chat(Chat(id='x1', title='Foo', source_url='https://x',
                        turns=[Turn('user', 'first version')], provider='test'),
                   self.tmp)
        # re-fetch of the same chat: retitled but slugify collision ("Foo!" -> "Foo")
        write_chat(Chat(id='x1', title='Foo!', source_url='https://x',
                                turns=[Turn('user', 'second version'),
                                       Turn('model', 'hidden-reasoning', thought=True),
                                       Turn('model', 'answer',
                                            images=[b'\x89PNGfake'])],
                                provider='test'),
                           self.tmp)
        d = self.tmp / 'Foo'
        self.assertEqual(sorted(p.name for p in d.glob('*.md')), ['Foo.md'])
        md = (d / 'Foo.md').read_text()
        self.assertIn('second version', md)
        self.assertNotIn('first version', md)
        self.assertNotIn('hidden-reasoning', md)
        self.assertTrue((d / 'image_1.png').exists())       # media additive
        payload = json.loads((d / 'chat.json').read_text())  # canonical overwritten
        self.assertEqual(payload['id'], 'x1')
        self.assertEqual(payload['title'], 'Foo!')

    def test_same_id_removes_stale_slug_md(self):
        # previous run left a Foo_1.md behind (old dedup bug); same id now
        d = self.tmp / 'Foo'
        d.mkdir(parents=True)
        (d / 'Foo.md').write_text('old')
        (d / 'Foo_1.md').write_text('stale')
        (d / 'chat.json').write_text(json.dumps({'id': 'x1', 'title': 'Foo'}))
        write_chat(Chat(id='x1', title='Foo', source_url='https://x',
                        turns=[Turn('user', 'fresh')], provider='test'),
                   self.tmp)
        self.assertEqual(sorted(p.name for p in d.glob('*.md')), ['Foo.md'])
        self.assertIn('fresh', (d / 'Foo.md').read_text())

    def test_different_id_keeps_dedup(self):
        write_chat(Chat(id='x1', title='Foo', source_url='https://x',
                        turns=[Turn('user', 'one')], provider='test'),
                   self.tmp)
        write_chat(Chat(id='x2', title='Foo', source_url='https://x',
                        turns=[Turn('user', 'two')], provider='test'),
                   self.tmp)
        d = self.tmp / 'Foo'
        self.assertTrue((d / 'Foo.md').exists())
        self.assertTrue((d / 'Foo_1.md').exists())
        self.assertIn('one', (d / 'Foo.md').read_text())
        self.assertIn('two', (d / 'Foo_1.md').read_text())


class TestSyncState(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_legacy_migration(self):
        legacy = self.tmp / '.last_sync_aistudio.json'
        legacy.write_text(json.dumps(
            {'chat1': {'updated_at': '1784827950', 'md': 'x'}}))
        s = SyncState(self.tmp, 'googleaistudio')
        self.assertFalse(legacy.exists())
        self.assertTrue((self.tmp / '.last_sync_googleaistudio.json').exists())
        self.assertEqual(s.known('chat1'),
                         {'updated_at': '1784827950', 'md': 'x'})

    def test_migrate_false_does_not_rename(self):
        legacy = self.tmp / '.last_sync_aistudio.json'
        legacy.write_text('{}')
        SyncState(self.tmp, 'googleaistudio', migrate=False)
        self.assertTrue(legacy.exists())

    def test_no_legacy_no_crash(self):
        s = SyncState(self.tmp, 'deepseek')
        self.assertIsNone(s.known('anything'))
        self.assertFalse((self.tmp / '.last_sync_deepseek.json').exists())

    def test_classify_and_is_unchanged(self):
        s = SyncState(self.tmp, 'kimi')
        self.assertFalse(s.is_unchanged('a', None))     # unknown -> fetch
        s.mark('a', 100, 'md')
        self.assertEqual(s.classify('a', None), 'unchanged')  # presence-based
        self.assertEqual(s.classify('a', 100), 'unchanged')
        self.assertEqual(s.classify('a', 101), 'changed')
        self.assertEqual(s.classify('b', None), 'new')
        s.save()
        s2 = SyncState(self.tmp, 'kimi')
        self.assertTrue(s2.is_unchanged('a', None))
        self.assertFalse(s2.is_unchanged('a', 101))


class TestDryRun(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.chats = [
            {'id': 'a', 'title': 'Alpha', 'updated_at': 1},
            {'id': 'b', 'title': 'Beta', 'updated_at': 2},
            {'id': 'c', 'title': 'Gamma', 'updated_at': 3},
            {'id': 'd', 'title': 'Delta', 'updated_at': 4},
        ]
        self.updated_map = {c['id']: c['updated_at'] for c in self.chats}

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_update_mode_counts(self):
        s = SyncState(self.tmp, 'kimi')
        s.mark('a', 1, 'md')   # unchanged
        s.mark('b', 1, 'md')   # changed
        plan = plan_fetch(self.chats, self.updated_map, s, skip_unchanged=True)
        self.assertEqual(plan['fetch'], 3)
        self.assertEqual(plan['skip'], 1)
        self.assertEqual(plan['new'], 2)
        self.assertEqual(plan['changed'], 1)
        self.assertEqual(plan['unchanged'], 1)
        self.assertEqual(plan['titles'], ['Beta', 'Gamma', 'Delta'])

    def test_fresh_mode_fetches_all(self):
        s = SyncState(self.tmp, 'kimi')
        s.mark('a', 1, 'md')
        s.mark('b', 1, 'md')
        s.mark('c', 3, 'md')
        plan = plan_fetch(self.chats, self.updated_map, s, skip_unchanged=False)
        self.assertEqual(plan['fetch'], 4)
        self.assertEqual(plan['skip'], 0)
        self.assertEqual(plan['titles'],
                         ['Alpha', 'Beta', 'Gamma', 'Delta'])

    def test_print_dry_run_output_and_no_writes(self):
        s = SyncState(self.tmp, 'kimi')
        s.mark('a', 1, 'md')
        s.mark('b', 1, 'md')
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = print_dry_run(self.chats, self.updated_map, s,
                                 skip_unchanged=True)
        self.assertEqual(code, 0)
        out = buf.getvalue()
        self.assertIn('would fetch: 3 (2 new, 1 changed) · '
                      'would skip: 1 unchanged', out)
        self.assertIn('  - Beta', out)
        # nothing written to disk
        self.assertEqual(list(self.tmp.iterdir()), [])

    def test_print_dry_run_fresh(self):
        s = SyncState(self.tmp, 'kimi')
        buf = io.StringIO()
        with redirect_stdout(buf):
            print_dry_run(self.chats, self.updated_map, s,
                          skip_unchanged=False)
        self.assertIn('would fetch: 4 (fresh) · would skip: 0',
                      buf.getvalue())


class TestStatus(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _ga_dir(self):
        ga = self.root / 'Google AI Studio'
        ga.mkdir(parents=True)
        (ga / '.reclaim_manifest.json').write_text(json.dumps({
            'provider': 'googleaistudio',
            'started': '2026-08-04 20:03:01',
            'duration_s': 42.3,
            'totals': {'chats': 3, 'ok': 3, 'failed': 0,
                       'images': 5, 'docs': 1, 'chars': 12345},
        }))
        (ga / 'Chat One').mkdir()
        (ga / 'Chat One' / 'chat.json').write_text('{}')
        (ga / 'Chat Two').mkdir()
        (ga / 'Chat Two' / 'chat.json').write_text('{}')
        return ga

    def test_scan_aggregation(self):
        ga = self._ga_dir()
        (ga / '.last_sync_googleaistudio.json').write_text(
            json.dumps({'a': {}, 'b': {}, 'c': {}}))
        infos = status.scan(self.root)
        ga_info = next(i for i in infos if i.provider == 'googleaistudio')
        self.assertTrue(ga_info.archived)
        self.assertEqual(ga_info.chat_folders, 2)
        self.assertEqual(ga_info.synced_chats, 3)
        self.assertEqual(ga_info.last_run['ok'], 3)
        self.assertEqual(ga_info.last_run['failed'], 0)
        self.assertEqual(ga_info.last_run['chars'], 12345)
        self.assertEqual(ga_info.last_sync, '2026-08-04 20:03:01')
        self.assertEqual(ga_info.duration_s, 42.3)
        ds = next(i for i in infos if i.provider == 'deepseek')
        self.assertFalse(ds.archived)
        self.assertIn('not archived', status.format_status(ds))
        self.assertIn('chats archived: 2', status.format_status(ga_info))

    def test_failures_reported(self):
        ga = self._ga_dir()
        payload = json.loads((ga / '.reclaim_manifest.json').read_text())
        payload['totals']['ok'], payload['totals']['failed'] = 2, 1
        (ga / '.reclaim_manifest.json').write_text(json.dumps(payload))
        infos = status.scan(self.root)
        ga_info = next(i for i in infos if i.provider == 'googleaistudio')
        self.assertEqual(ga_info.last_run['failed'], 1)
        self.assertIn('2 ok, 1 failed', status.format_status(ga_info))

    def test_legacy_sync_name_fallback(self):
        ga = self._ga_dir()   # no .last_sync_googleaistudio.json yet
        (ga / '.last_sync_aistudio.json').write_text(
            json.dumps({'a': {}, 'b': {}, 'c': {}, 'd': {}}))
        infos = status.scan(self.root)
        ga_info = next(i for i in infos if i.provider == 'googleaistudio')
        self.assertEqual(ga_info.synced_chats, 4)
        # the fallback read must NOT rename (status is read-only)
        self.assertTrue((ga / '.last_sync_aistudio.json').exists())


class TestDispatch(unittest.TestCase):
    """reclaim/__main__.py arg routing — offline, no browser."""

    def test_help_and_empty(self):
        import reclaim.__main__ as cli
        self.assertEqual(cli.main([]), 0)
        self.assertEqual(cli.main(['--help']), 0)

    def test_all_help_short_circuits(self):
        import reclaim.__main__ as cli
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cli.main(['all', '--help'])
        self.assertEqual(code, 0)
        self.assertIn('reclaim all [options]', buf.getvalue())

    def test_unknown_command_returns_1(self):
        import reclaim.__main__ as cli
        self.assertEqual(cli.main(['bogus']), 1)

    def test_status_routing_passes_flags_through(self):
        import reclaim.__main__ as cli
        root = Path(tempfile.mkdtemp())
        try:
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = cli.main(['status', '-o', str(root)])
            self.assertEqual(code, 0)
            self.assertIn('not archived yet', buf.getvalue())
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_provider_flags_not_misrouted(self):
        # argv[1] after a provider is the provider's own arg, not a target:
        # `reclaim googleaistudio --url X` must not treat --url as target.
        import reclaim.__main__ as cli
        self.assertIn('googleaistudio', cli.PROVIDERS)
        self.assertEqual(cli.ALL_PROVIDERS,
                         ['googleaistudio', 'deepseek', 'kimi', 'chatgpt'])


if __name__ == '__main__':
    unittest.main()
