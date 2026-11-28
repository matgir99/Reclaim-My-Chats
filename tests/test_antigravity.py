"""Google Antigravity: conversations read from the folder Antigravity keeps.

Every conversation here is synthetic: the steps are written with a small
protobuf encoder using the field numbers from antigravity_steps.py.
"""

from __future__ import annotations

import base64
import json
import os
import sqlite3
import tempfile
import time
import unittest
import warnings
from pathlib import Path
from unittest.mock import patch

from reclaim.core import engine, locks
from reclaim.core.events import ProgressEvent, routed
from reclaim.core.service import ArchiveController, uses_browser
from reclaim.providers import antigravity
from reclaim.providers import antigravity_steps as S

PNG = b'\x89PNG\r\n\x1a\n' + b'\0' * 16
REAL_HOLD = locks.hold


def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        byte, n = n & 0x7F, n >> 7
        out.append(byte | (0x80 if n else 0))
        if not n:
            return bytes(out)


def f(number: int, value: int | str | bytes) -> bytes:
    """One protobuf field: an int as a varint, text or bytes length-delimited."""
    if isinstance(value, int):
        return _varint(number << 3) + _varint(value)
    data = value.encode('utf-8') if isinstance(value, str) else value
    return _varint(number << 3 | 2) + _varint(len(data)) + data


def step(kind: int, body_field: int, body: bytes, *, status: int = 3, meta: bytes = b'') -> bytes:
    return (f(S.STEP_TYPE, kind) + f(S.STEP_STATUS, status)
            + f(S.STEP_METADATA, f(S.META_CREATED_AT, f(S.TIME_SECONDS, 1_790_000_000)) + meta)
            + f(body_field, body))


def user(text: str, *images: bytes) -> bytes:
    body = f(S.INPUT_TEXT, text) + f(S.INPUT_ITEMS, f(S.ITEM_TEXT, text))
    body += b''.join(f(S.INPUT_IMAGES, f(S.IMAGE_BASE64, base64.b64encode(i).decode())
                       + f(S.IMAGE_MIME, 'image/png')) for i in images)
    return step(S.USER_INPUT, S.STEP_USER_INPUT, body)


def planner(response: str = '', thinking: str = '') -> bytes:
    body = f(S.PLAN_RESPONSE, response) if response else b''
    return step(S.PLANNER_RESPONSE, S.STEP_PLANNER,
                body + (f(S.PLAN_THINKING, thinking) if thinking else b''))


def tool(name: str, args: dict[str, str] | None = None, *, prefix: str = '',
         parts: tuple[bytes, ...] = (), status: int = 3, result: str = '',
         hidden: bool = False) -> bytes:
    render = (f(S.RENDER_PREFIX, prefix) if prefix else b'') + b''.join(
        f(S.RENDER_PARTS, p) for p in parts) + (f(S.RENDER_HIDDEN, 1) if hidden else b'')
    body = b''.join(f(S.GENERIC_ARGS, f(S.ENTRY_KEY, k) + f(S.ENTRY_VALUE, v))
                    for k, v in (args or {}).items())
    body += f(S.GENERIC_RESULT, f(S.RESULT_TEXT, result) + f(S.RESULT_RENDER, render))
    return step(S.GENERIC, S.STEP_GENERIC, body, status=status,
                meta=f(S.META_TOOL_CALL, f(S.CALL_NAME, name)))


def text_part(text: str) -> bytes:
    return f(S.PART_TEXT, text)


def file_part(uri: str) -> bytes:
    return f(S.PART_FILE, f(S.FILE_URI, uri))


def diff_part(added: int, deleted: int) -> bytes:
    return f(S.PART_FILE_DIFF, f(S.DIFF_STATS, f(S.STATS_ADDED, added) + f(S.STATS_DELETED, deleted)))


def agent_part(conversation: str, title: str) -> bytes:
    return f(S.PART_AGENT, f(S.AGENT_CONVERSATION, conversation) + f(S.AGENT_TITLE, title))


def error(message: str) -> bytes:
    return step(S.ERROR_MESSAGE, S.STEP_ERROR_MESSAGE,
                f(S.ERROR_DETAILS, f(S.DETAILS_USER_MESSAGE, message)))


def system(event: str, text: str) -> bytes:
    return step(S.SYSTEM_MESSAGE, S.STEP_SYSTEM_MESSAGE,
                f(S.SYSTEM_TEXT, text) + (f(S.SYSTEM_EVENT, event) if event else b''))


def checkpoint(title: str) -> bytes:
    return step(S.CHECKPOINT, S.STEP_CHECKPOINT, f(S.CHECKPOINT_TITLE, title))


def turns_of(*steps: bytes, parent_id: str = '', parent_title: str = ''):
    transcript = S.Transcript(parent_id=parent_id, parent_title=parent_title)
    for payload in steps:
        transcript.add(S.Message(payload))
    return transcript.turns()


ID = {name: f'{n:08x}-0000-4000-8000-{n:012x}' for n, name in enumerate(
    ('plain', 'renamed', 'untitled', 'parent', 'child', 'other', 'broken'), 1)}


class Store:
    """A synthetic Antigravity folder: <home>/antigravity and <home>/config."""

    def __init__(self, home: Path):
        self.root = home / 'antigravity'
        (self.root / 'conversations').mkdir(parents=True)
        (self.root / 'annotations').mkdir()
        (home / 'config' / 'projects').mkdir(parents=True)
        self.projects = home / 'config' / 'projects'
        with sqlite3.connect(self.root / 'conversation_summaries.db') as con:
            con.execute('CREATE TABLE conversation_summaries (conversation_id TEXT PRIMARY KEY, '
                        'title TEXT, project_id TEXT, parent_conversation_id TEXT, '
                        'agent_name TEXT, last_modified_time TEXT)')
        con.close()

    def project(self, pid: str, name: str) -> None:
        (self.projects / f'{pid}.json').write_text(json.dumps({'id': pid, 'name': name}),
                                                   encoding='utf-8')

    def add(self, cid: str, steps: list[bytes], *, title: str = '', project: str = '',
            parent: str = '', agent: str = '', source: int = 22, when: int = 0) -> Path:
        path = self.root / 'conversations' / f'{cid}.db'
        with sqlite3.connect(path) as con:
            con.execute('CREATE TABLE trajectory_meta (trajectory_id TEXT, cascade_id TEXT, '
                        'trajectory_type INTEGER, source INTEGER)')
            con.execute('INSERT INTO trajectory_meta VALUES (?, ?, 4, ?)', ('t-' + cid, cid, source))
            con.execute('CREATE TABLE steps (idx INTEGER PRIMARY KEY, step_type INTEGER, '
                        'status INTEGER, step_payload BLOB)')
            for idx, payload in enumerate(steps):
                try:
                    message = S.Message(payload)
                    kind, status = message.num(S.STEP_TYPE), message.num(S.STEP_STATUS)
                except S.FormatError:
                    kind, status = 0, 0
                con.execute('INSERT INTO steps VALUES (?, ?, ?, ?)', (idx, kind, status, payload))
        con.close()
        modified = time.strftime('%Y-%m-%d %H:%M:%S+00:00', time.gmtime(when)) if when else ''
        with sqlite3.connect(self.root / 'conversation_summaries.db') as con:
            con.execute('INSERT INTO conversation_summaries VALUES (?, ?, ?, ?, ?, ?)',
                        (cid, title, project, parent, agent, modified))
        con.close()
        return path

    def annotate(self, cid: str, text: str) -> None:
        (self.root / 'annotations' / f'{cid}.pbtxt').write_text(text, encoding='utf-8')

    def artifact(self, cid: str, name: str, data: bytes) -> None:
        folder = self.root / 'brain' / cid
        folder.mkdir(parents=True, exist_ok=True)
        (folder / name).write_bytes(data)


class TestSteps(unittest.TestCase):
    def test_truncated_or_mistyped_steps_are_a_format_error(self):
        with self.assertRaises(S.FormatError):
            S.Message(user('hello')[:-3])
        with self.assertRaises(S.FormatError):
            S.Message(f(S.STEP_TYPE, 'not a number')).num(S.STEP_TYPE)
        with self.assertRaises(S.FormatError):
            S.Message(b'\x0b')  # wire type 3 (groups) is not used

    def test_turns_tool_lines_thinking_and_errors(self):
        turns = turns_of(
            user('check the keys'),
            planner(thinking='private reasoning'),
            tool('run_command', {'CommandLine': 'lsusb'}, prefix='Ran',
                 parts=(text_part('check USB devices'),)),
            tool('edit_file', prefix='Edited',
                 parts=(file_part('file:///home/u/My%20Repo/main.py'), diff_part(3, 1))),
            tool('view_file', {'AbsolutePath': '/home/u/notes.md'}),
            tool('run_command', {'toolSummary': 'stop the server'}, status=S.CANCELED),
            tool('run_command', prefix='Ran', parts=(text_part('hidden'),), hidden=True),
            planner(response='Both keys are fine.'),
            error('Individual quota reached.'),
            checkpoint('USB keys'),
            system('work_efficiently_reminder', 'be brief'),
            user('thanks'))
        self.assertEqual([t.role for t in turns], ['user', 'model', 'user'])
        model = turns[1]
        self.assertEqual(model.text, (
            '- *Ran check USB devices*\n'
            '- *Edited `main.py` (+3 −1)*\n'
            '- *Viewed `notes.md`*\n'
            '- *Ran stop the server (canceled)*\n\n'
            'Both keys are fine.\n\n'
            '> Antigravity: Individual quota reached.'))
        self.assertTrue(model.had_thoughts)
        self.assertNotIn('private reasoning', model.text)
        self.assertEqual(model.error, 'Individual quota reached.')

    def test_plan_questions_answers_and_images(self):
        questions = json.dumps([{'question': 'Which folder?', 'options': ['Keep', 'Move']}])
        turns = turns_of(
            user('set it up', PNG),
            tool('exit_plan_mode', {'plan': 'Step 1. Read the docs.'}),
            tool('ask_question', {'questions': questions},
                 result=('Created At: now\nCompleted At: later\nA1: Use these settings:\n'
                         'port: 8080\nhost: localhost\nA2: Keep it\n')),
            planner(response='Keeping the folder.'))
        self.assertEqual([t.role for t in turns], ['user', 'model', 'user', 'model'])
        self.assertEqual(turns[0].images, [PNG])
        self.assertIn('**Plan**\n\nStep 1. Read the docs.', turns[1].text)
        self.assertIn('1. Which folder?\n   - Keep\n   - Move', turns[1].text)
        self.assertEqual(turns[2].text, 'A1: Use these settings:\nport: 8080\nhost: localhost'
                                        '\n\nA2: Keep it')
        self.assertEqual(turns[3].text, 'Keeping the folder.')

    def test_file_uris_become_paths_of_this_system(self):
        self.assertEqual(S.native_path('file:///home/u/my%20image.png'), '/home/u/my image.png')
        self.assertEqual(S.native_path('/home/u/image.png'), '/home/u/image.png')
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', DeprecationWarning)
            import nturl2path  # what urllib.request uses on Windows
        with patch.object(S, 'url2pathname', nturl2path.url2pathname):
            self.assertEqual(S.native_path('file:///C:/Users/u/my%20image.png'),
                             'C:\\Users\\u\\my image.png')
            self.assertEqual(S.native_path('file://server/share/image.png'),
                             '\\\\server\\share\\image.png')

    def test_a_subagent_gets_its_instructions_from_the_parent_and_reports_back(self):
        parent = ID['parent']
        turns = turns_of(
            system('agent_message', f'[Message] timestamp=t sender={parent} priority=HIGH '
                                    'content=Audit the repository.\nList the bugs.'),
            system('', '[Message] timestamp=t sender=system priority=LOW content=[Notice] done'),
            tool('send_message', {'Recipient': parent, 'Message': '# Report\n\nNo bugs.'},
                 prefix='Messaged', parts=(text_part('Parent Agent'),)),
            parent_id=parent, parent_title='Plan the audit')
        self.assertEqual(turns[0].role, 'user')
        self.assertEqual(turns[0].text, '*From the agent of “Plan the audit”:*\n\n'
                                        'Audit the repository.\nList the bugs.')
        self.assertEqual(turns[1].text, '- *Messaged Parent Agent*\n\n'
                                        '**Message to the parent agent**\n\n# Report\n\nNo bugs.')
        # In the parent's own conversation, the report is not repeated.
        self.assertEqual(turns_of(system('agent_message', f'[Message] sender={ID["child"]} '
                                                         'content=# Report')), [])


class TestProvider(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.home = Path(self._temp.name)
        self.store = Store(self.home / 'gemini')
        env = patch.dict(os.environ, {antigravity.ENV_DIR: str(self.store.root)})
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(self._temp.cleanup)
        self.app = self.home / 'app'
        self.app.mkdir()

    def fill(self) -> None:
        store = self.store
        store.project('p1', 'Home Server')
        store.add(ID['plain'], [user('fix the printer'), planner(response='Done.')],
                  title='Printer Fix', project='p1', when=1_790_000_300)
        store.add(ID['renamed'], [user('hello'), planner(response='Hi.')],
                  title='Old Title', when=1_790_000_200)
        store.annotate(ID['renamed'],
                       'title:"Caf\\303\\251 \\"plans\\"" last_user_view_time:{seconds:1}\n')
        store.add(ID['untitled'], [user('what is   the\nweather'), planner(response='Sunny.')],
                  when=1_790_000_100, source=17)
        store.add(ID['parent'], [user('audit it'),
                                 tool('invoke_subagent', prefix='Started',
                                      parts=(agent_part(ID['child'], 'Code Auditor'),)),
                                 planner(response='Audit started.')],
                  title='Audit', project='p1', when=1_790_000_050)
        store.add(ID['child'], [system('agent_message', f'[Message] sender={ID["parent"]} '
                                                        'content=Find bugs.'),
                                planner(response='Found none.')],
                  project='p1', parent=ID['parent'], agent='research', when=1_790_000_040)
        store.artifact(ID['plain'], 'walkthrough.md', b'# Walkthrough\n')
        store.artifact(ID['plain'], 'walkthrough.md.metadata.json', b'{}')
        store.artifact(ID['plain'], '.hidden', b'x')
        (self.store.root / 'conversations' / f'{ID["other"]}.pb').write_bytes(b'\x00encrypted')

    def update(self, *argv: str) -> tuple[int, list[ProgressEvent]]:
        events: list[ProgressEvent] = []
        with (patch('reclaim.core.service.browser.launch',
                    side_effect=AssertionError('no browser for Antigravity')),
              patch('reclaim.core.service.locks.hold', wraps=REAL_HOLD) as hold):
            mode = argv[0] if argv else 'update'
            summary = ArchiveController(self.app).run(['antigravity'], mode, events.append)
        self.assertNotIn('browser', [call.args[1] for call in hold.call_args_list])
        return summary.exit_code, events

    def test_listing_titles_projects_subagents_and_old_files(self):
        self.fill()
        session = antigravity.SPEC.open(None)
        events: list[ProgressEvent] = []
        with routed(events.append):
            refs = antigravity.SPEC.listing(session)
        self.assertEqual([r.title for r in refs], [
            'Printer Fix', 'Café "plans"', 'what is the weather', 'Audit',
            'Code Auditor (research subagent)'])
        self.assertEqual([r.project for r in refs],
                         ['Home Server', None, None, 'Home Server', 'Home Server'])
        self.assertTrue(all(r.updated_at.startswith('2026-') and ' steps · ' in r.updated_at
                            for r in refs))
        warnings = [e.message for e in events if e.kind == 'warning']
        self.assertEqual(len(warnings), 1)
        self.assertIn('1 conversation(s) saved by an earlier Antigravity version', warnings[0])

    def test_update_saves_every_conversation_without_a_browser(self):
        self.fill()
        code, _ = self.update()
        self.assertEqual(code, 0)
        base = self.app / 'chats' / 'Google Antigravity'
        md = (base / 'Home Server' / 'Printer Fix' / 'Printer Fix.md').read_text(encoding='utf-8')
        self.assertIn(f'**Source:** Antigravity for VS Code, conversation {ID["plain"]}', md)
        self.assertIn('**Attachment:** [walkthrough.md](walkthrough.md)', md)
        self.assertTrue((base / 'Home Server' / 'Printer Fix' / 'walkthrough.md').is_file())
        self.assertFalse((base / 'Home Server' / 'Printer Fix' / '.hidden').exists())
        cli_md = (base / 'what is the weather' / 'what is the weather.md').read_text(
            encoding='utf-8')
        self.assertIn('**Source:** Antigravity CLI, conversation', cli_md)
        child = base / 'Home Server' / 'Code Auditor research subagent'
        child_md = next(child.glob('*.md')).read_text(encoding='utf-8')
        self.assertIn('*From the agent of “Audit”:*\n\nFind bugs.', child_md)
        raw = json.loads((child / 'raw.json').read_text(encoding='utf-8'))
        self.assertEqual([s['type'] for s in raw['steps']], ['system_message', 'planner_response'])
        # Nothing changed: the next update saves nothing.
        code, events = self.update()
        self.assertEqual(code, 0)
        self.assertFalse([e for e in events if e.kind == 'chat_completed'])

    def test_a_conversation_removed_from_antigravity_is_filed_as_deleted(self):
        self.fill()
        self.assertEqual(self.update()[0], 0)
        (self.store.root / 'conversations' / f'{ID["untitled"]}.db').unlink()
        self.assertEqual(antigravity.SPEC.confirm_status(antigravity.SPEC.open(None),
                                                         ID['untitled']), 'deleted')
        self.assertEqual(self.update()[0], 0)
        base = self.app / 'chats' / 'Google Antigravity'
        self.assertTrue((base / '_Deleted' / 'what is the weather').is_dir())
        self.assertFalse((base / 'what is the weather').exists())

    def test_a_conversation_that_does_not_parse_fails_alone(self):
        self.fill()
        self.store.add(ID['broken'], [user('fine'), b'\x08\x0e\x2a\x05ab'], title='Broken')
        code, events = self.update()
        self.assertEqual(code, 2)
        errors = [e for e in events if e.kind == 'error']
        self.assertEqual(len(errors), 1)
        self.assertIn('in a format this version cannot read', errors[0].message)
        self.assertTrue((self.app / 'chats' / 'Google Antigravity' / 'Home Server'
                         / 'Printer Fix').is_dir())

    def test_reading_creates_nothing_in_antigravitys_folder(self):
        self.fill()
        folder = self.store.root / 'conversations'
        with sqlite3.connect(folder / f'{ID["plain"]}.db') as con:
            con.execute('PRAGMA journal_mode=WAL')  # as Antigravity's files are
        con.close()
        before = sorted(p.name for p in folder.iterdir())
        self.assertEqual(self.update()[0], 0)
        self.assertEqual(sorted(p.name for p in folder.iterdir()), before)
        # A log left without its index (only -wal): a copy is read instead.
        (folder / f'{ID["plain"]}.db-wal').write_bytes(b'')
        session = antigravity.SPEC.open(None)
        with routed(lambda _event: None):
            ref = antigravity.SPEC.from_url(session, ID['plain'])
        self.assertEqual(antigravity.SPEC.fetch(session, ref).chat.title, 'Printer Fix')
        self.assertFalse((folder / f'{ID["plain"]}.db-shm').exists())

    def test_only_a_change_in_steps_or_artifacts_saves_again(self):
        self.fill()
        self.assertEqual(self.update()[0], 0)
        path = self.store.root / 'conversations' / f'{ID["plain"]}.db'
        os.utime(path, (2_000_000_000, 2_000_000_000))  # rewritten, no step changed
        saved = [e.title for e in self.update()[1] if e.kind == 'chat_completed']
        self.assertEqual(saved, [])
        self.store.artifact(ID['plain'], 'walkthrough.md', b'# Walkthrough, revised\n')
        saved = [e.title for e in self.update()[1] if e.kind == 'chat_completed']
        self.assertEqual(saved, ['Printer Fix'])
        md = self.app / 'chats' / 'Google Antigravity' / 'Home Server' / 'Printer Fix'
        self.assertIn(b'revised', (md / 'walkthrough.md').read_bytes())

    def test_an_artifact_that_cannot_be_read_is_linked_and_tried_again(self):
        self.fill()
        real_read = Path.read_bytes

        def locked(path: Path) -> bytes:
            if path.name == 'walkthrough.md':
                raise PermissionError(13, 'Permission denied', str(path))
            return real_read(path)

        folder = self.app / 'chats' / 'Google Antigravity' / 'Home Server' / 'Printer Fix'
        for _ in range(2):  # saved each time, as long as it cannot be read
            with patch.object(Path, 'read_bytes', locked):
                code, events = self.update()
            self.assertEqual(code, 0)
            self.assertIn('Printer Fix', [e.title for e in events if e.kind == 'chat_completed'])
            warnings_ = [e.message for e in events if e.kind == 'warning']
            self.assertTrue(any('walkthrough.md (Permission denied)' in m for m in warnings_))
            md = (folder / 'Printer Fix.md').read_text(encoding='utf-8')
            self.assertIn('**Attachment (not downloaded):** [walkthrough.md](file://', md)
            self.assertFalse((folder / 'walkthrough.md').exists())
        saved = [e.title for e in self.update()[1] if e.kind == 'chat_completed']
        self.assertEqual(saved, ['Printer Fix'])  # readable again: saved with the file
        self.assertTrue((folder / 'walkthrough.md').is_file())
        self.assertEqual([e for e in self.update()[1] if e.kind == 'chat_completed'], [])

    def test_a_subagent_of_a_subagent_names_its_parent_the_same_way_every_time(self):
        store = self.store
        top, mid, low = (f'{n}0000000-0000-4000-8000-000000000000' for n in (5, 9, 1))
        store.add(top, [user('plan it'), tool('invoke_subagent', prefix='Started', parts=(
            agent_part(mid, 'Planner'),))], title='Top')
        store.add(mid, [system('agent_message', f'[Message] sender={top} content=Plan.'),
                        tool('invoke_subagent', prefix='Started',
                             parts=(agent_part(low, 'Checker'),))],
                  parent=top, agent='research')
        store.add(low, [system('agent_message', f'[Message] sender={mid} content=Check.'),
                        planner(response='Checked.')], parent=mid, agent='research')
        found, _ = antigravity.list_conversations(store.root)
        named = {c.id: (c.title, c.parent_title) for c in found}
        self.assertEqual(named[mid], ('Planner (research subagent)', 'Top'))
        self.assertEqual(named[low], ('Checker (research subagent)',
                                      'Planner (research subagent)'))

    def test_a_missing_folder_stops_with_what_to_do(self):
        with patch.dict(os.environ, {antigravity.ENV_DIR: str(self.home / 'nowhere')}):
            with self.assertRaises(RuntimeError) as caught:
                antigravity.SPEC.open(None)
        self.assertIn(antigravity.ENV_DIR, str(caught.exception))

    def test_one_conversation_by_id(self):
        self.fill()
        session = antigravity.SPEC.open(None)
        with routed(lambda _event: None):  # the warning about the old .pb file
            ref = antigravity.SPEC.from_url(session, f'/x/conversations/{ID["renamed"]}.db')
        self.assertEqual(ref.title, 'Café "plans"')
        got = antigravity.SPEC.fetch(session, ref)
        self.assertEqual([t.text for t in got.chat.turns], ['hello', 'Hi.'])
        with self.assertRaises(ValueError):
            antigravity.SPEC.from_url(session, 'https://example.com/chat')


class TestRunsWithoutABrowser(unittest.TestCase):
    def test_without_a_providers_list_antigravity_runs_only_when_it_is_here(self):
        from reclaim.core import config
        from reclaim.core.service import ALL_PROVIDERS, present
        with tempfile.TemporaryDirectory() as temp:
            root, store = Path(temp), Store(Path(temp) / 'gemini')
            with patch.dict(os.environ, {antigravity.ENV_DIR: str(root / 'nowhere')}):
                chosen, skipped, _ = config.selected_providers(root, ALL_PROVIDERS,
                                                               present=present)
                self.assertNotIn('antigravity', chosen)
                self.assertEqual(skipped, ['antigravity'])
                config.save(root, providers=['antigravity'], archive=None)
                chosen, _, _ = config.selected_providers(root, ALL_PROVIDERS, present=present)
                self.assertEqual(chosen, ['antigravity'])  # named: runs, and says what is wrong
            (root / '.reclaim.json').unlink()
            with patch.dict(os.environ, {antigravity.ENV_DIR: str(store.root)}):
                chosen, _, _ = config.selected_providers(root, ALL_PROVIDERS, present=present)
            self.assertEqual(chosen, ALL_PROVIDERS)

    def test_a_browser_that_cannot_start_fails_only_the_web_providers(self):
        with tempfile.TemporaryDirectory() as temp:
            store = Store(Path(temp) / 'gemini')
            store.add(ID['plain'], [user('hi'), planner(response='Hello.')], title='Hi')
            launches: list[int] = []

            def launch(*_args, **_kwargs):
                launches.append(1)
                raise RuntimeError('No Chrome or Chromium was found')

            class _Playwright:
                def __enter__(self):
                    return self

                def __exit__(self, *_):
                    return False

            events: list[ProgressEvent] = []
            with (patch.dict(os.environ, {antigravity.ENV_DIR: str(store.root)}),
                  patch('reclaim.core.service.browser.launch', side_effect=launch),
                  patch('playwright.sync_api.sync_playwright', return_value=_Playwright())):
                summary = ArchiveController(Path(temp) / 'app').run(
                    ['chatgpt', 'antigravity', 'claude'], 'update', events.append)
        self.assertEqual(summary.codes, (('chatgpt', 2), ('antigravity', 0), ('claude', 2)))
        self.assertEqual(launches, [1])  # not tried again for Claude
        errors = {e.provider: e.message for e in events if e.kind == 'error'}
        self.assertIn('No Chrome or Chromium was found', errors['chatgpt'])
        self.assertIn('the browser could not start', errors['claude'])

    def test_capture_is_refused_and_only_web_providers_open_the_browser(self):
        self.assertFalse(uses_browser('antigravity'))
        self.assertTrue(uses_browser('chatgpt'))
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(ValueError) as caught:
                ArchiveController(Path(temp)).capture('antigravity', 'some-id')
        self.assertIn('read from files on this computer', str(caught.exception))

    def test_a_mixed_run_opens_the_browser_only_for_web_providers(self):
        pages: list[object] = []
        launched: list[int] = []

        class _Web:
            SPEC = engine.Provider()

            @staticmethod
            def parse_args(argv):
                return engine.parse_args(engine.Provider(), argv)

            @staticmethod
            def run_session(page, args, sink=None):
                pages.append(page)
                return 0

        class _Local(_Web):
            SPEC = antigravity.SPEC

        class _Ctx:
            def close(self):
                pass

        class _Playwright:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

        def launch(*_args, **_kwargs):
            launched.append(1)
            return _Ctx(), 'page'

        modules = {'antigravity': _Local, 'chatgpt': _Web, 'claude': _Web}
        with tempfile.TemporaryDirectory() as temp:
            with (patch('reclaim.core.service.provider_module', side_effect=modules.__getitem__),
                  patch('reclaim.core.service.browser.launch', side_effect=launch),
                  patch('playwright.sync_api.sync_playwright', return_value=_Playwright())):
                controller = ArchiveController(Path(temp))
                summary = controller.run(['antigravity'])
                self.assertEqual((pages, launched), ([None], []))
                summary = controller.run(['antigravity', 'chatgpt', 'claude'])
        self.assertEqual(summary.exit_code, 0)
        self.assertEqual(pages, [None, None, 'page', 'page'])
        self.assertEqual(launched, [1])


if __name__ == '__main__':
    unittest.main()
