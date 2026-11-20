"""Captures: real responses with the chats' text replaced (core/capture.py).

All inputs here are synthetic. The rules were also checked against a real
database of 583 chats from all six providers: every parser read each
cleaned chat into the same turns, roles, attachments and text lengths."""

import base64
import contextlib
import io
import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from reclaim import __main__ as cli
from reclaim.core import capture, engine, retry
from reclaim.core.batchexecute import decode_response
from reclaim.core.events import ProgressEvent
from reclaim.core.model import Chat, Turn
from reclaim.core.service import ArchiveController
from reclaim.providers import chatgpt, claude, deepseek, googleaistudio, googlegemini, kimi

FIX = Path(__file__).resolve().parent / 'fixtures'
PRIVATE = ['Zanzibar', 'Ottoline', 'ottoline.hatch@example.org', '+39 333 123 4567',
           'Via Garibaldi 17', 'tachycardia']


def cleaned(*values, seed=7):
    sanitizer = capture.Sanitizer(seed=seed)
    sanitizer.prepare(values)
    return [sanitizer.clean(value) for value in values]


def words(text: str) -> set[str]:
    return set(re.findall(r'[^\W_]+', text))


class TextTests(unittest.TestCase):
    def test_prose_keeps_its_shape_but_none_of_its_words(self):
        text = 'Ciao Ottoline! My **cardiologist** said: tachycardia (2 episodes).'
        [out] = cleaned({'text': text})
        out = out['text']
        self.assertEqual(len(out), len(text))
        self.assertEqual(re.sub(r'[^\W_]', '', out), re.sub(r'[^\W_]', '', text))
        self.assertFalse(words(text) & words(out))

    def test_private_details_never_survive(self):
        record = {'title': 'Trip to Zanzibar',
                  'messages': [{'role': 'user', 'content': f'I am {p}, see you'} for p in PRIVATE],
                  'meta': {'Ottoline': 'ottoline.hatch@example.org'}}
        dump = json.dumps(cleaned(record)[0], ensure_ascii=False)
        for secret in PRIVATE:
            self.assertNotIn(secret, dump)
        self.assertNotIn('Ottoline', dump)

    def test_codes_numbers_and_dates_stay(self):
        value = {'role': 'assistant', 'content_type': 'multimodal_text',
                 'type': 'BrandNewKind', 'finish_reason': 'stop', 'size': 1234,
                 'seconds': '1700000000', 'created_at': '2026-01-02T03:04:05.123Z',
                 'ok': True, 'none': None}
        self.assertEqual(cleaned(value)[0], value)

    def test_field_names_stay_and_id_keys_follow_their_values(self):
        data = {'mapping': {'node-aa1': {'parent': None, 'children': ['node-bb2']},
                            'node-bb2': {'parent': 'node-aa1', 'children': []}},
                'current_node': 'node-bb2'}
        [out] = cleaned(data)
        self.assertEqual(set(out), {'mapping', 'current_node'})
        leaf = out['current_node']
        self.assertNotEqual(leaf, 'node-bb2')
        root = out['mapping'][leaf]['parent']
        self.assertEqual(out['mapping'][root]['children'], [leaf])

    def test_different_strings_stay_different(self):
        ids = [f'n{i}' for i in range(300)]
        [out] = cleaned({'ids': ids})
        self.assertEqual(len(set(out['ids'])), 300)

    def test_same_seed_same_result(self):
        value = {'text': 'Something private', 'id': 'abc-123'}
        self.assertEqual(cleaned(value, seed=3), cleaned(value, seed=3))
        self.assertNotEqual(cleaned(value, seed=3), cleaned(value, seed=4))


class ReferenceTests(unittest.TestCase):
    """Strings that refer to each other must still match after cleaning."""

    def test_chatgpt_citation_markers_match_their_references(self):
        cite, short = 'citeturn0search3', 'map'
        message = {'content': {'parts': [f'Ottoline lives here.{cite} Map:{short}']},
                   'metadata': {'content_references': [{'matched_text': cite},
                                                       {'matched_text': short}]}}
        [out] = cleaned(message)
        part = out['content']['parts'][0]
        for ref in out['metadata']['content_references']:
            self.assertIn(ref['matched_text'], part)

    def test_asset_pointer_keeps_its_prefix_and_file_id(self):
        data = {'parts': [{'asset_pointer': 'file-service://file-Ab12Cd34'}],
                'attachments': [{'id': 'file-Ab12Cd34', 'name': 'holiday.png'}]}
        [out] = cleaned(data)
        pointer = out['parts'][0]['asset_pointer']
        found = chatgpt._ASSET_RE.search(pointer)
        assert found is not None
        self.assertEqual(found.group(1), out['attachments'][0]['id'])
        self.assertTrue(out['attachments'][0]['name'].endswith('.png'))
        self.assertNotIn('holiday', out['attachments'][0]['name'])

    def test_deepseek_citation_index_stays(self):
        [out] = cleaned({'content': 'As shown [citation:2] and [citation:14].'})
        self.assertEqual(re.findall(r'\[citation:(\d+)\]', out['content']), ['2', '14'])

    def test_a_role_name_does_not_match_inside_a_word(self):
        [out] = cleaned({'role': 'system', 'text': 'These systems work'})
        self.assertNotIn('system', out['text'])

    def test_links_keep_their_scheme(self):
        [out] = cleaned({'url': 'https://example.org/Ottoline/report'})
        self.assertTrue(out['url'].startswith('https://'))
        self.assertNotIn('Ottoline', out['url'])


class PictureAndJsonTests(unittest.TestCase):
    def test_pictures_become_the_sample_picture(self):
        photo = base64.b64encode(b'\x89PNG\r\n\x1a\n' + bytes(range(256)) * 4).decode()
        [out] = cleaned({'data': photo, 'src': f'data:image/jpeg;base64,{photo}'})
        self.assertEqual(out['data'], capture.sample_png())
        self.assertEqual(out['src'], 'data:image/jpeg;base64,' + capture.sample_png())
        self.assertTrue(base64.b64decode(capture.sample_png()).startswith(b'\x89PNG'))
        self.assertGreater(len(capture.sample_png()), 500)  # parsers skip tiny inline images

    def test_json_inside_a_string_is_cleaned_and_stays_json(self):
        inner = json.dumps([['Ottoline', 'c_abc123'], {'role': 'model'}])
        [out] = cleaned({'payload': inner, 'id': 'c_abc123'})
        decoded = json.loads(out['payload'])
        self.assertNotIn('Ottoline', out['payload'])
        self.assertEqual(decoded[0][1], out['id'])
        self.assertEqual(decoded[1], {'role': 'model'})


class VocabularyTests(unittest.TestCase):
    def test_holds_the_parsers_words_but_nothing_from_this_computer(self):
        vocab = capture.vocabulary()
        self.assertIn(googlegemini.READ_RPC, vocab.values)
        self.assertIn('wrb.fr', vocab.values)
        self.assertIn('citation', vocab.words)
        home = str(Path.home())
        self.assertFalse(any(home in s for s in vocab.values))
        self.assertNotIn(Path.home().name, vocab.words)


def shape(chat):
    return [(t.role, t.thought, t.had_thoughts, len(t.text), bool(t.error), len(t.images),
             [a.kind for a in t.attachments]) for t in chat.turns]


class FixtureRoundTripTests(unittest.TestCase):
    """Each parser reads a cleaned fixture into the same shape."""

    def check(self, name, parse):
        data = json.loads((FIX / name).read_text())
        [out] = cleaned(data)
        self.assertEqual(shape(parse(out)), shape(parse(data)), name)

    def test_every_provider(self):
        self.check('deepseek_sample.json', lambda d: deepseek.record_to_chat(d['data']))
        self.check('claude_sample.json', lambda d: claude.parse_chat(d, 'x', 'u'))
        self.check('kimi_messages_sample.json',
                   lambda d: kimi.parse_chat({'id': 'k'}, list(reversed(d['messages'])))[0])
        self.check('gemini_read_sample.json', lambda d: googlegemini.read_to_chat(
            googlegemini.parse_read(d), 'x', 'u'))
        self.check('aistudio_sample.json', lambda d: googleaistudio.entries_to_chat(
            googleaistudio.parse_rpc(d), 'x', 'u'))

        def chatgpt_chat(d):
            turns = [chatgpt._node_to_turn(n)[0]
                     for n in chatgpt._linearize(d['mapping'], d['current_node'])]
            return type('C', (), {'turns': [t for t in turns if t is not None]})
        self.check('chatgpt_api_sample.json', chatgpt_chat)


class CaptureFileTests(unittest.TestCase):
    def test_save_and_load(self):
        read = json.loads((FIX / 'gemini_read_sample.json').read_text())
        frame = json.dumps([['wrb.fr', googlegemini.READ_RPC, json.dumps(read), None, None,
                             None, 'generic']])
        responses = [
            {'request': 'Claude GET https://claude.ai/api/chat/conv-claude-0001',
             'status': 200, 'text': (FIX / 'claude_sample.json').read_text()},
            {'request': f'Gemini RPC {googlegemini.READ_RPC}', 'status': 200,
             'text': f")]}}'\n\n{len(frame)}\n{frame}\n"},
            {'request': 'Kimi chat', 'status': 500, 'text': 'Ottoline broke it'},
        ]
        try:
            decode_response('Ottoline is not a reply')
            raise AssertionError('expected an error')
        except RuntimeError as exc:  # the error the capture records
            error = exc
        with tempfile.TemporaryDirectory() as tmp:
            path = capture.save(Path(tmp) / 'c.json', 'googlegemini', responses, error)
            document = json.loads(path.read_text())
            loaded = capture.load(path)
        self.assertEqual(document['format'], capture.FORMAT)
        self.assertEqual([r['body_format'] for r in document['responses']],
                         ['json', 'batchexecute', 'text'])
        self.assertNotIn('Ottoline', path.name + json.dumps(document))
        self.assertTrue(document['responses'][0]['request'].startswith('Claude GET https://'))
        self.assertNotIn('conv-claude-0001', document['responses'][0]['request'])
        assert loaded.error is not None
        self.assertEqual(loaded.error['type'], type(error).__name__)
        self.assertIn('reclaim/core/batchexecute.py', loaded.error['where'])
        self.assertIn('unrecognized batchexecute response', loaded.error['message'])
        # The bodies read back as the provider code reads them.
        chat = claude.parse_chat(json.loads(loaded.responses[0].text), 'x', 'u')
        self.assertTrue(chat.turns)
        payload = decode_response(loaded.responses[1].text, googlegemini.READ_RPC)
        self.assertEqual(shape(googlegemini.read_to_chat(googlegemini.parse_read(payload), 'x', 'u')),
                         shape(googlegemini.read_to_chat(googlegemini.parse_read(read), 'x', 'u')))
        self.assertEqual(loaded.responses[2].status, 500)

    def test_load_refuses_other_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'x.json'
            path.write_text('{"format": "something else"}')
            with self.assertRaises(ValueError):
                capture.load(path)

    def test_default_path_is_in_the_data_folder(self):
        path = capture.default_path(Path('/data'), 'kimi')
        self.assertEqual(path.parent, Path('/data/captures'))
        self.assertTrue(path.name.startswith('kimi-') and path.suffix == '.json')


class _Context:
    def close(self):
        pass


class _Playwright:
    def __enter__(self):
        return object()

    def __exit__(self, *_):
        return False


class _RecordingProvider(engine.Provider):
    name = 'claude'
    folder = 'Claude'
    pace = 0.0

    def __init__(self, *, fail: bool = False, extra: dict | None = None):
        self.fail = fail
        self.extra = extra or {}

    def open(self, page):
        return None

    def from_url(self, session, url: str) -> engine.ChatRef:
        return engine.ChatRef(id=url.rsplit('/', 1)[-1])

    def fetch(self, session, ref: engine.ChatRef) -> engine.Fetched:
        retry.send(lambda: {'status': 200, 'text': json.dumps({'name': 'Ottoline chat',
                                                              'chat_messages': []})},
                   what='Claude GET chat')
        if self.fail:
            raise RuntimeError('Ottoline response shape changed')
        return engine.Fetched(Chat(id=ref.id, title='T', source_url='', provider='claude',
                                   turns=[Turn('user', 'hi')]), extra=dict(self.extra))


class CaptureWorkflowTests(unittest.TestCase):
    def test_engine_capture_saves_on_success_failure_and_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ok_dest = root / 'ok.json'
            events: list[ProgressEvent] = []
            code = engine.capture(_RecordingProvider(), None, 'https://claude.ai/chat/c1',
                                  ok_dest, events.append)
            self.assertEqual(code, 0)
            loaded = capture.load(ok_dest)
            self.assertEqual(len(loaded.responses), 1)
            self.assertIsNone(loaded.error)
            self.assertNotIn('Ottoline', ok_dest.read_text(encoding='utf-8'))
            self.assertEqual([e.kind for e in events], ['captured'])
            self.assertEqual(events[0].detail, str(ok_dest))

            fail_dest = root / 'fail.json'
            fail_events: list[ProgressEvent] = []
            code = engine.capture(_RecordingProvider(fail=True), None, 'https://claude.ai/chat/c1',
                                  fail_dest, fail_events.append)
            self.assertEqual(code, 0)
            loaded_fail = capture.load(fail_dest)
            assert loaded_fail.error is not None
            self.assertEqual(loaded_fail.error['type'], 'RuntimeError')
            self.assertEqual([e.kind for e in fail_events], ['warning', 'captured'])
            self.assertIn('capture was still saved', fail_events[0].message)

            fb_events: list[ProgressEvent] = []
            engine.capture(_RecordingProvider(extra={'path': 'intercept'}), None,
                           'https://aistudio.google.com/prompts/p1', root / 'fb.json',
                           fb_events.append)
            self.assertEqual([e.kind for e in fb_events], ['warning', 'captured'])
            self.assertIn('intercept fallback', fb_events[0].message)

    def test_aistudio_drive_token_is_not_recorded(self):
        page = MagicMock()
        token_payload = json.dumps(['ya29.' + 'a' * 80])
        chat_payload = json.dumps([[[], None, None, None, ['Title'], None, None, None,
                                    None, None, None, None, None, []]])
        page.evaluate.side_effect = [
            {'status': 200, 'text': token_payload, 'retryAfter': None},
            {'status': 200, 'text': chat_payload, 'retryAfter': None},
        ]
        with retry.recording() as recorded:
            drive = googleaistudio.DriveClient(page)
            self.assertTrue(drive.token().startswith('ya29.'))
            googleaistudio.rpc_call(page, 'ResolveDriveResource', ['p1'])
        self.assertEqual(len(recorded), 1)
        self.assertIn('ResolveDriveResource', recorded[0]['request'])
        self.assertNotIn('ya29.', json.dumps(recorded))

    def test_controller_capture_locks_profile_only_and_saves_default_or_custom(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            module = SimpleNamespace(SPEC=_RecordingProvider())
            with (patch('reclaim.core.service.provider_module', return_value=module),
                  patch('reclaim.core.service.browser.launch',
                        return_value=(_Context(), object())),
                  patch('playwright.sync_api.sync_playwright', return_value=_Playwright())):
                summary = ArchiveController(root).capture('claude', 'https://claude.ai/chat/c1')
            self.assertEqual(summary.exit_code, 0)
            self.assertFalse((root / 'chats').exists())
            self.assertTrue((root / '.playwright-profile' / '.reclaim.lock').exists())
            captures = list((root / 'captures').glob('claude-*.json'))
            self.assertEqual(len(captures), 1)

    def test_cli_capture_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dest = root / 'out.json'
            module = SimpleNamespace(SPEC=_RecordingProvider(fail=True))
            out = io.StringIO()
            with (patch.dict(os.environ, {'RECLAIM_HOME': str(root)}),
                  patch('reclaim.core.service.provider_module', return_value=module),
                  patch('reclaim.core.service.browser.launch',
                        return_value=(_Context(), object())),
                  patch('playwright.sync_api.sync_playwright', return_value=_Playwright()),
                  contextlib.redirect_stdout(out)):
                code = cli.main(['capture', 'claude', 'https://claude.ai/chat/c1',
                                 '-o', str(dest)])
            self.assertEqual(code, 0)
            self.assertTrue(dest.exists())
            self.assertIn(str(dest), out.getvalue())
            self.assertIn('check that nothing private remains', out.getvalue())

            # Failure before saving (e.g., destination is an existing directory) -> exit 2.
            bad_dest = root / 'existing_dir'
            bad_dest.mkdir()
            err_out = io.StringIO()
            with (patch.dict(os.environ, {'RECLAIM_HOME': str(root)}),
                  patch('reclaim.core.service.provider_module', return_value=module),
                  patch('reclaim.core.service.browser.launch',
                        return_value=(_Context(), object())),
                  patch('playwright.sync_api.sync_playwright', return_value=_Playwright()),
                  contextlib.redirect_stdout(err_out)):
                code = cli.main(['capture', 'claude', 'https://claude.ai/chat/c1',
                                 '-o', str(bad_dest)])
            self.assertEqual(code, 2)
            self.assertNotIn('check that nothing private remains', err_out.getvalue())


if __name__ == '__main__':
    unittest.main()
