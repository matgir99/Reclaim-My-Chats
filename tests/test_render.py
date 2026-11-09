"""Progress events and their renderers (core/events.py, core/render.py). Offline."""

from __future__ import annotations

import contextlib
import io
import logging
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from reclaim.core import engine, events, logs, render
from reclaim.core.events import ProgressEvent, emit
from reclaim.core.model import Chat, Turn
from reclaim.core.service import ArchiveController


def event(kind, **fields) -> ProgressEvent:
    return ProgressEvent(kind, **fields)


class TestWording(unittest.TestCase):
    def test_describe_keeps_outcomes_and_drops_per_chat_progress(self):
        self.assertIsNone(render.describe(event('chat_started', title='A', current=1, total=2)))
        self.assertIsNone(render.describe(event('chat_skipped', title='A')))
        self.assertEqual(render.describe(event('chat_completed', title='A', message='2t, 10 chars')),
                         'A — saved (2t, 10 chars)')
        self.assertEqual(render.describe(event('error', title='A', message='HTTP 500')),
                         'A — FAILED: HTTP 500')
        self.assertEqual(render.describe(event('error', message='timeout')), 'FAILED: timeout')
        self.assertEqual(render.describe(event('plan', message='would fetch: 1', detail='  - A')),
                         'would fetch: 1\n  - A')
        self.assertEqual(render.describe(event('downloaded', message='Done: 1 ok, 0 failed',
                                               detail='Manifest: m.json')),
                         'Done: 1 ok, 0 failed · Manifest: m.json')
        self.assertEqual(render.describe(event('login_needed', message='Log in to Kimi')),
                         'Log in to Kimi')


class TestTerminal(unittest.TestCase):
    def lines(self, renderer, *evs) -> str:
        out = io.StringIO()
        renderer.stream = out
        for e in evs:
            renderer(e)
        return out.getvalue()

    def test_summary_level_by_default(self):
        evs = [event('provider_started', provider='kimi', current=1, total=1),
               event('chat_started', title='A', current=1, total=1),
               event('chat_completed', title='A', current=1, total=1, message='2t, 9 chars',
                     detail='assets: 0 · 0.1s'),
               event('error', title='B' * 80, current=2, total=2, message='HTTP 500',
                     detail='Traceback (most recent call last): ...'),
               event('warning', title='C', message='raw.json was not saved'),
               event('plan', message='would fetch: 1', detail='  - A')]
        text = self.lines(render.TerminalRenderer(), *evs)
        self.assertNotIn('kimi', text)          # no heading for a single provider
        self.assertNotIn('2t, 9 chars', text)   # per-chat lines need --log
        self.assertIn(f'[2/2] {"B" * 55} -> FAILED: HTTP 500', text)
        self.assertNotIn('Traceback', text)     # kept for the log and --log
        self.assertIn('  warning: C: raw.json was not saved', text)
        self.assertIn('would fetch: 1\n  - A', text)

    def test_verbose_adds_chat_lines_progress_and_tracebacks(self):
        renderer = render.TerminalRenderer(verbose=True, headers=True)
        text = self.lines(renderer,
                          event('provider_started', provider='kimi', current=1, total=1),
                          event('downloading', total=2, message='2 chats'),
                          event('chat_started', title='A', current=1, total=2),
                          event('chat_completed', title='A', current=1, total=2,
                                message='2t, 9 chars', detail='assets: 0 · 0.1s'),
                          event('error', provider='kimi', message='HTTP 401',
                                detail='Traceback: boom\n'),
                          event('plan', message='would fetch: 1', detail='  - A'))
        self.assertIn('========== kimi ==========', text)
        self.assertIn('[1/2]  50% · elapsed', text)
        self.assertIn('[1/2] A -> 2t, 9 chars\n    assets: 0 · 0.1s', text)
        self.assertIn('kimi: FAILED: HTTP 401\nTraceback: boom', text)
        self.assertNotIn('  - A', text)         # per-chat lines replace the title list

    def test_login_request_stands_out(self):
        text = self.lines(render.TerminalRenderer(),
                          event('login_needed', provider='kimi', message='Log in to Kimi'))
        self.assertIn(f'{render.RULE}\n  Log in to Kimi\n{render.RULE}', text)


class TestLog(unittest.TestCase):
    def test_levels_tracebacks_and_no_per_chat_progress(self):
        logger = logging.getLogger('reclaim.test-render')
        with self.assertLogs(logger, logging.INFO) as captured:
            renderer = render.LogRenderer(logger)
            renderer(event('chat_started', provider='kimi', title='A', current=1, total=1))
            renderer(event('chat_completed', provider='kimi', title='A', message='2t'))
            renderer(event('warning', provider='kimi', title='A', message='no raw.json'))
            renderer(event('error', provider='kimi', title='B', message='HTTP 500',
                           detail='Traceback: boom\n'))
        self.assertEqual(captured.output, [
            'INFO:reclaim.test-render:kimi: A — saved (2t)',
            'WARNING:reclaim.test-render:kimi: warning: A — no raw.json',
            'ERROR:reclaim.test-render:kimi: B — FAILED: HTTP 500',
            'ERROR:reclaim.test-render:Traceback: boom',
        ])


class TestRouting(unittest.TestCase):
    def test_route_reaches_nested_code_and_is_restored(self):
        outer: list[ProgressEvent] = []
        inner: list[ProgressEvent] = []
        with events.routed(outer.append):
            emit(None, 'notice', message='one')
            with events.routed(inner.append):
                emit(None, 'notice', message='two')
            emit(None, 'notice', message='three')
        self.assertEqual([e.message for e in outer], ['one', 'three'])
        self.assertEqual([e.message for e in inner], ['two'])
        self.assertIsNone(events.routed_sink())

    def test_route_belongs_to_its_thread(self):
        seen: list[ProgressEvent] = []
        other = io.StringIO()

        def elsewhere():
            with contextlib.redirect_stdout(other):
                emit(None, 'notice', message='from another thread')
        with events.routed(seen.append):
            thread = threading.Thread(target=elsewhere)
            thread.start()
            thread.join()
        self.assertEqual(seen, [])
        self.assertIn('from another thread', other.getvalue())  # unrouted: the terminal

    def test_an_explicit_sink_wins_and_fanout_keeps_order(self):
        first: list[str] = []
        second: list[str] = []
        both = render.fanout(lambda e: first.append(e.message), None,
                             lambda e: second.append(e.message))
        with events.routed(lambda e: self.fail('the route must not be used')):
            emit(both, 'notice', message='x')
        self.assertEqual((first, second), (['x'], ['x']))


class _Fake(engine.Provider):
    name = 'chatgpt'
    folder = 'ChatGPT'
    pace = 0

    def open(self, page):
        emit(None, 'login_needed', self.name, message='Log in to Fake in the browser window.')
        emit(None, 'logged_in', self.name, message='Logged in.')
        return None

    def listing(self, session):
        return [engine.ChatRef('a', 'Alpha', '1'), engine.ChatRef('b', 'Beta', '1')]

    def fetch(self, session, ref):
        if ref.id == 'b':
            raise RuntimeError('synthetic failure')
        return engine.Fetched(Chat(id=ref.id, title=ref.title, source_url='', provider='fake',
                                   turns=[Turn('user', 'q'), Turn('model', 'a')]))


class _Module:
    SPEC = _Fake()

    @classmethod
    def parse_args(cls, argv):
        return engine.parse_args(cls.SPEC, argv)

    @classmethod
    def run_session(cls, page, args, sink=None):
        return engine.run_session(cls.SPEC, page, args, sink)


class _Context:
    def close(self):
        pass


class _Playwright:
    def __enter__(self):
        return object()

    def __exit__(self, *_):
        return False


class TestOneRenderingEach(unittest.TestCase):
    """A command's events reach the terminal once and the log file once."""

    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)

    def tearDown(self):
        self._temp.cleanup()

    def run_command(self, *argv) -> str:
        out = io.StringIO()
        with (patch('reclaim.core.service.provider_module', return_value=_Module()),
              patch('reclaim.core.service.browser.launch', return_value=(_Context(), object())),
              patch('playwright.sync_api.sync_playwright', return_value=_Playwright()),
              contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO())):
            ArchiveController(self.root).run_cli(
                ['chatgpt'], [*argv, '-o', str(self.root / 'chats' / 'ChatGPT')])
        return out.getvalue()

    def test_terminal_and_log(self):
        printed = self.run_command()
        self.assertEqual(printed.count('Log in to Fake in the browser window.'), 1)
        self.assertEqual(printed.count('Beta -> FAILED: synthetic failure'), 1)
        self.assertEqual(printed.count('Done: 1 ok, 1 failed'), 1)
        self.assertNotIn('Traceback', printed)
        self.assertNotIn('Alpha', printed)  # per-chat lines need --log
        logged = logs.log_file(self.root).read_text(encoding='utf-8')
        self.assertEqual(logged.count('Log in to Fake in the browser window.'), 1)
        self.assertEqual(logged.count('chatgpt: Alpha — saved (2t, '), 1)
        self.assertEqual(logged.count('chatgpt: Beta — FAILED: synthetic failure'), 1)
        self.assertEqual(logged.count('Traceback (most recent call last)'), 1)
        self.assertEqual(logged.count('Done: 1 ok, 1 failed'), 1)

    def test_log_option_shows_chat_lines_once(self):
        printed = self.run_command('--log', '--rebuild')
        self.assertEqual(printed.count('[1/2] Alpha -> 2t, '), 1)
        self.assertEqual(printed.count('Traceback (most recent call last)'), 1)


if __name__ == '__main__':
    unittest.main()
