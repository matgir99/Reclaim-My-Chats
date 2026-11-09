"""The provider engine, through a fake provider: filters, --list, --dry-run,
skipping unchanged chats, failure isolation, URL mode and the run manifest.
Offline and synthetic."""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from reclaim.core import engine
from reclaim.core.manifest import SyncState
from reclaim.core.model import Chat, Turn


class FakeProvider(engine.Provider):
    name = 'fake'
    folder = 'Fake'
    description = 'A provider for tests.'
    pace = 0.0

    def __init__(self, chats: dict[str, tuple[str, str]], failing=()):
        self.chats = chats          # id -> (title, updated_at)
        self.failing = set(failing)
        self.fetched: list[str] = []
        self.opened = 0

    def open(self, page):
        self.opened += 1
        return {'page': page}

    def listing(self, session):
        return [engine.ChatRef(id=cid, title=title, updated_at=updated)
                for cid, (title, updated) in self.chats.items()]

    def from_url(self, session, url):
        return engine.ChatRef(id=url.rsplit('/', 1)[-1])

    def fetch(self, session, ref):
        self.fetched.append(ref.id)
        if ref.id in self.failing:
            raise RuntimeError(f'synthetic failure for {ref.id}')
        title = self.chats.get(ref.id, (f'Chat {ref.id}', None))[0]
        chat = Chat(id=ref.id, title=title, source_url=f'https://fake.test/{ref.id}',
                    provider='fake', turns=[Turn('user', 'question'), Turn('model', 'answer')])
        return engine.Fetched(chat, raw={'id': ref.id}, detail='fake detail', extra={'path': 'api'})


class TestEngine(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.out = Path(self._temp.name) / 'Fake'
        self.provider = FakeProvider({'a': ('Alpha notes', '1'), 'b': ('Beta', '1'),
                                      'c': ('Gamma notes', '1')})

    def tearDown(self):
        self._temp.cleanup()

    def run_engine(self, *argv):
        args = engine.parse_args(self.provider, [*argv, '-o', str(self.out)])
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(io.StringIO()):
            code = engine.run_session(self.provider, None, args)
        return code, buffer.getvalue()

    def test_update_saves_new_chats_then_skips_unchanged_ones(self):
        code, out = self.run_engine()
        self.assertEqual(code, 0)
        self.assertIn('Done: 3 ok, 0 failed', out)
        self.assertTrue((self.out / 'Beta' / 'Beta.md').exists())
        self.assertEqual(json.loads((self.out / 'Beta' / 'raw.json').read_text()), {'id': 'b'})
        manifest = json.loads((self.out / '.reclaim_manifest.json').read_text())
        self.assertEqual(manifest['chats'][0]['path'], 'api')
        self.provider.fetched.clear()
        self.provider.chats['b'] = ('Beta', '2')  # edited at the provider
        code, out = self.run_engine()
        self.assertEqual(self.provider.fetched, ['b'])

    def test_title_skip_and_limit_select_chats(self):
        self.run_engine('notes', '--limit', '1')
        self.assertEqual(self.provider.fetched, ['a'])
        self.provider.fetched.clear()
        self.run_engine('--rebuild', '--skip', '1')
        self.assertEqual(self.provider.fetched, ['b', 'c'])

    def test_list_and_dry_run_write_nothing(self):
        code, out = self.run_engine('--list')
        self.assertEqual(code, 0)
        self.assertIn('2. Beta', out)
        code, out = self.run_engine('--dry-run')
        self.assertEqual(code, 0)
        self.assertIn('would fetch: 3 (3 new, 0 changed)', out)
        self.assertEqual(self.provider.fetched, [])
        self.assertFalse(self.out.exists())

    def test_one_failure_does_not_stop_the_others_and_is_retried_later(self):
        self.provider.failing = {'b'}
        code, out = self.run_engine()
        self.assertEqual(code, 2)
        self.assertIn('Beta -> FAILED: synthetic failure for b', out)
        self.assertIn('Done: 2 ok, 1 failed', out)
        sync = SyncState(self.out, 'fake')
        self.assertIsNone(sync.known('b'))
        self.provider.failing.clear()
        self.provider.fetched.clear()
        self.run_engine()
        self.assertEqual(self.provider.fetched, ['b'])

    def test_url_and_no_match(self):
        code, _ = self.run_engine('--url', 'https://fake.test/c/zeta')
        self.assertEqual(code, 0)
        self.assertEqual(self.provider.fetched, ['zeta'])
        self.assertTrue((self.out / 'Chat zeta').exists())
        code, out = self.run_engine('no such title')
        self.assertEqual(code, 1)
        self.assertIn('No chats matched.', out)

    def test_title_and_url_together_are_refused(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            engine.parse_args(self.provider, ['Alpha', '--url', 'https://fake.test/c/a'])

    def test_log_mode_and_pacing(self):
        self.provider.pace = 0.25
        with patch.object(engine.time, 'sleep') as sleep:
            _, out = self.run_engine('--log')
        self.assertIn('[2/3] Beta -> 2t,', out)
        self.assertIn('fake detail', out)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [0.25, 0.25, 0.25])


if __name__ == '__main__':
    unittest.main()
