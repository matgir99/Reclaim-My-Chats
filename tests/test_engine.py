"""The provider engine, through a fake provider: filters, --list, --dry-run,
skipping unchanged chats, failure isolation, URL mode and the run manifest.
Offline and synthetic."""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from reclaim.core import browser, config, engine
from reclaim.core.events import ProgressEvent
from reclaim.core.manifest import SyncState
from reclaim.core.model import Chat, Turn
from reclaim.core.service import ArchiveController, RunSummary


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

    def test_module_entry_point_uses_the_process_arguments(self):
        # `python -m reclaim.providers.<name> --dry-run` calls main() without argv.
        calls = []

        def run_cli(_controller, providers, argv, *, headers=True):
            calls.append(argv)
            return RunSummary(((providers[0], 0),))
        with (patch.object(ArchiveController, 'run_cli', run_cli),
              patch.object(sys, 'argv', ['chatgpt.py', '--dry-run', '--limit', '1'])):
            self.assertEqual(engine.main(self.provider), 0)
            self.assertEqual(engine.main(self.provider, []), 0)
        self.assertEqual(calls, [['--dry-run', '--limit', '1'], []])

    def test_log_mode_and_pacing(self):
        self.provider.pace = 0.25
        with patch.object(engine.time, 'sleep') as sleep:
            _, out = self.run_engine('--log')
        self.assertIn('[2/3] Beta -> 2t,', out)
        self.assertIn('fake detail', out)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [0.25, 0.25, 0.25])

    def test_download_stops_after_current_chat_when_stop_is_set(self):
        stop = threading.Event()
        provider = FakeProvider({'a': ('Alpha', '1'), 'b': ('Beta', '1')})
        provider.pace = 0.5
        events: list[ProgressEvent] = []
        original_fetch = provider.fetch

        def stopping_fetch(session, ref):
            got = original_fetch(session, ref)
            stop.set()
            return got

        with (patch.object(provider, 'fetch', side_effect=stopping_fetch),
              patch.object(engine.time, 'sleep') as sleep):
            refs = provider.listing(None)
            results = engine.download(provider, None, refs, self.out,
                                      sink=events.append, stop=stop)
        self.assertEqual([r['id'] for r in results], ['a'])
        self.assertEqual(provider.fetched, ['a'])
        self.assertTrue((self.out / 'Alpha' / 'Alpha.md').exists())
        self.assertFalse((self.out / 'Beta').exists())
        self.assertIsNotNone(SyncState(self.out, 'fake').known('a'))
        self.assertIsNone(SyncState(self.out, 'fake').known('b'))
        sleep.assert_not_called()
        stopped = [e for e in events if e.kind == 'stopped']
        self.assertEqual(len(stopped), 1)
        self.assertEqual(stopped[0].message, 'Stopped after 1 of 2 chats')

    def test_controller_stop_ends_current_provider_and_skips_next(self):
        with tempfile.TemporaryDirectory() as root_dir:
            root = Path(root_dir)
            controller = ArchiveController(root)
            events: list[ProgressEvent] = []
            seen: list[str] = []

            class _FakeCtx:
                def close(self):
                    pass

            class _FakePlaywright:
                def __enter__(self):
                    return self

                def __exit__(self, *_):
                    pass

            class _StoppingProvider:
                def __init__(self, prov_name: str):
                    self._spec = FakeProvider({'a': ('Alpha', '1'), 'b': ('Beta', '1')})
                    self._spec.name = prov_name
                    self._spec.folder = prov_name.capitalize()

                def parse_args(self, argv):
                    return engine.parse_args(self._spec, argv)

                def run_session(self, page, args, sink=None):
                    seen.append(self._spec.name)
                    original = self._spec.fetch

                    def fetch_and_stop(session, ref):
                        got = original(session, ref)
                        controller.stop()
                        return got

                    with patch.object(self._spec, 'fetch', side_effect=fetch_and_stop):
                        return engine.run_session(self._spec, page, args, sink)

            modules = {'chatgpt': _StoppingProvider('chatgpt'),
                       'claude': _StoppingProvider('claude')}
            with (patch('reclaim.core.service.provider_module', side_effect=modules.__getitem__),
                  patch('reclaim.core.service.browser.launch', return_value=(_FakeCtx(), object())),
                  patch('playwright.sync_api.sync_playwright', return_value=_FakePlaywright())):
                summary = controller.run(['chatgpt', 'claude'], sink=events.append)
            self.assertEqual(seen, ['chatgpt'])
            self.assertEqual(summary.codes, (('chatgpt', 0),))
            self.assertEqual(modules['chatgpt']._spec.fetched, ['a'])
            self.assertTrue((root / 'chats' / 'ChatGPT' / 'Alpha' / 'Alpha.md').exists())
            self.assertTrue(any(e.kind == 'stopped' and e.provider == 'chatgpt' for e in events))

    def test_wait_for_login_and_bring_browser_forward(self):
        class _Page:
            def __init__(self):
                self.fronted = 0
                self.context = self

            def bring_to_front(self):
                self.fronted += 1

        page = _Page()
        with patch.object(browser, 'set_window_bounds'):
            self.assertTrue(browser.bring_browser_forward(page))
        self.assertEqual(page.fronted, 1)

        with patch.object(browser, 'ask_for_login') as ask:
            self.assertEqual(browser.wait_for_login(page, 'chatgpt', 'ChatGPT', lambda: 'tok'), 'tok')
        ask.assert_not_called()

        attempts = iter([RuntimeError('not yet'), 'tok-2'])
        events: list[ProgressEvent] = []

        def probe():
            val = next(attempts)
            if isinstance(val, Exception):
                raise val
            return val

        with (patch.object(browser, 'login_timeout', return_value=30),
              patch.object(browser, 'set_window_bounds'),
              patch.object(browser.time, 'sleep'),
              patch('reclaim.core.browser.emit', side_effect=lambda _s, k, p='', message='':
                    events.append(ProgressEvent(k, p, message=message)))):
            self.assertEqual(browser.wait_for_login(page, 'kimi', 'Kimi', probe, interval=0), 'tok-2')
        self.assertEqual([e.kind for e in events], ['login_needed', 'logged_in'])

        with (patch.object(browser, 'login_timeout', return_value=-1),
              patch.object(browser, 'ask_for_login'),
              self.assertRaisesRegex(RuntimeError, 'Login timeout')):
            browser.wait_for_login(page, 'kimi', 'Kimi', lambda: None)

    def test_login_polling_does_not_sleep_past_the_deadline(self):
        page = object()
        with (patch.object(browser, 'login_timeout', return_value=30),
              patch.object(browser, 'ask_for_login'),
              patch.object(browser, 'set_window_bounds') as bounds,
              patch.object(browser.time, 'monotonic', side_effect=[0, 29.5, 29.5, 30]),
              patch.object(browser, '_sleep_stopping', return_value=False) as sleep,
              self.assertRaisesRegex(RuntimeError, 'Login timeout for Kimi after 30 seconds')):
            browser.wait_for_login(page, 'kimi', 'Kimi', lambda: None)
        sleep.assert_called_once_with(0.5, page)
        bounds.assert_called_once_with(page, state='minimized')

    def test_google_login_timeout_reports_the_configured_wait(self):
        class _Page:
            url = 'https://accounts.google.com/signin'

        page = _Page()
        with (patch.object(browser, 'login_timeout', return_value=30),
              patch.object(browser, 'ask_for_login') as ask,
              patch.object(browser, 'set_window_bounds') as bounds,
              patch.object(browser.time, 'monotonic', side_effect=[0, 29.875, 30]),
              patch.object(browser, '_wait_url_slice', return_value=False) as wait,
              self.assertRaisesRegex(RuntimeError, 'Login timeout for Google after 30 seconds')):
            browser.interactive_login(page, 'accounts.google.com', 'https://example.test', 'Google')
        ask.assert_called_once_with(page, '', 'Google', 30)
        wait.assert_called_once_with(page, 'https://example.test', 125)
        bounds.assert_called_once_with(page, state='minimized')

    def test_cross_thread_bring_browser_forward_wait_for_login_and_clear(self):
        worker_tid = threading.get_ident()
        owner_calls: list[int] = []
        caller_results: list[bool] = []
        calls_before_sleep = [0]
        attempts = 0

        class _Page:
            context = None

            def bring_to_front(self):
                owner_calls.append(threading.get_ident())

        def probe():
            nonlocal attempts
            attempts += 1
            if attempts == 2:
                t = threading.Thread(target=lambda: caller_results.append(
                    browser.bring_browser_forward()))
                t.start()
                t.join(timeout=2)
                calls_before_sleep[0] = len(owner_calls)
            return 'tok' if attempts >= 3 else None

        page = _Page()
        try:
            with (patch.object(browser, 'login_timeout', return_value=30),
                  patch.object(browser, 'set_window_bounds'),
                  patch.object(browser.time, 'sleep'),
                  patch('reclaim.core.browser.emit')):
                got = browser.wait_for_login(page, 'chatgpt', 'ChatGPT', probe, interval=0)
            self.assertEqual(got, 'tok')
            self.assertEqual(caller_results, [True])
            self.assertEqual(calls_before_sleep[0], 1)  # caller thread did not touch page
            self.assertEqual(owner_calls, [worker_tid, worker_tid])
            browser.clear_active_page()
            self.assertFalse(browser.bring_browser_forward())
        finally:
            browser.clear_active_page()

    def test_cross_thread_bring_browser_forward_interactive_login(self):
        worker_tid = threading.get_ident()
        owner_calls: list[int] = []
        caller_results: list[bool] = []
        ready = threading.Event()
        caller_done = threading.Event()
        slices = 0

        class _Page:
            url = 'https://accounts.google.com/signin'
            context = None

            def bring_to_front(self):
                owner_calls.append(threading.get_ident())

            def wait_for_url(self, ready_url, timeout=0):
                nonlocal slices
                slices += 1
                if slices == 1:
                    ready.set()
                    caller_done.wait(timeout=2)
                    raise TimeoutError('timeout')
                return True

        def caller():
            ready.wait(timeout=2)
            caller_results.append(browser.bring_browser_forward())
            caller_done.set()

        page = _Page()
        t = threading.Thread(target=caller)
        t.start()
        try:
            with (patch.object(browser, 'login_timeout', return_value=30),
                  patch.object(browser, 'set_window_bounds'),
                  patch.object(browser.time, 'sleep'),
                  patch('reclaim.core.browser.emit')):
                browser.interactive_login(page, 'accounts.google.com', lambda _: True, 'Google')
            t.join(timeout=2)
            self.assertEqual(caller_results, [True])
            self.assertEqual(owner_calls, [worker_tid, worker_tid])
        finally:
            caller_done.set()
            browser.clear_active_page()

    def test_login_wait_cancellation_and_controller_root_isolation(self):
        stop = threading.Event()
        events: list[ProgressEvent] = []

        class _Page:
            url = 'https://accounts.google.com/signin'
            context = None

            def bring_to_front(self):
                pass

            def wait_for_url(self, ready_url, timeout=0):
                stop.set()
                raise TimeoutError('timeout')

        page = _Page()
        with (patch.object(browser, 'set_window_bounds') as bounds,
              patch.object(browser, 'login_timeout', return_value=30),
              patch('reclaim.core.browser.emit'),
              engine.stoppable(stop)):
            with self.assertRaises(browser.LoginCancelledError):
                browser.interactive_login(page, 'accounts.google.com', lambda _: False, 'Google')
            with self.assertRaises(browser.LoginCancelledError):
                browser.wait_for_login(page, 'kimi', 'Kimi', lambda: None, interval=0)
        bounds.assert_called_with(page, state='minimized')

        class _ClosedPage:
            url = 'https://kimi.ai/'
            context = None

            def is_closed(self):
                return True

        with (patch.object(browser, 'login_timeout', return_value=30),
              patch('reclaim.core.browser.emit')):
            with self.assertRaises(browser.LoginCancelledError):
                browser.wait_for_login(_ClosedPage(), 'kimi', 'Kimi', lambda: None, interval=0)

        # ArchiveController(custom_root) binds config.use_root and passes root to browser.launch.
        with tempfile.TemporaryDirectory() as root_dir:
            root = Path(root_dir)
            config.save(root, providers=['kimi'], archive=None,
                        kimi_region='mainland', chrome_path='/custom/chrome', login_timeout=180)
            observed: dict = {}

            class _FakeCtx:
                def close(self):
                    pass

            class _FakePlaywright:
                def __enter__(self):
                    return self

                def __exit__(self, *_):
                    pass

            class _Mod:
                def parse_args(self, argv):
                    return engine.parse_args(FakeProvider({}), argv)

                def run_session(self, page, args, sink=None):
                    observed['kimi'] = config.kimi_base_url()
                    observed['timeout'] = browser.login_timeout()
                    observed['chrome'] = browser.find_chrome()
                    raise browser.LoginCancelledError('stopped')

            ctrl = ArchiveController(root)

            def fake_launch(pw, headless=False, profile_dir=None, *, root=None):
                observed['launch_root'] = root
                return _FakeCtx(), object()

            with (patch('reclaim.core.service.provider_module', return_value=_Mod()),
                  patch('reclaim.core.service.browser.launch', side_effect=fake_launch),
                  patch('playwright.sync_api.sync_playwright', return_value=_FakePlaywright())):
                summary = ctrl.run(['kimi'], sink=events.append)
            self.assertEqual(summary.codes, (('kimi', 0),))
            self.assertEqual(observed['launch_root'], root)
            self.assertEqual(observed['kimi'], 'https://www.kimi.com')
            self.assertEqual(observed['timeout'], 180)
            self.assertEqual(observed['chrome'], '/custom/chrome')
            self.assertTrue(any(e.kind == 'stopped' and e.provider == 'kimi' for e in events))


if __name__ == '__main__':
    unittest.main()
