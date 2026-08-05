"""Offline parser/writer tests — no browser required.

Run: python3.14 -m unittest discover tests   (or ./run_tests.sh)
"""

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from typing import ClassVar

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from reclaim.core.model import Attachment, Chat, Turn
from reclaim.core.writer import img_ext, slugify, write_chat
from reclaim.providers.googleaistudio import entries_to_chat, parse_rpc
from reclaim.providers.aistudio_files import parse_prompt_file
from reclaim.providers.chatgpt_import import parse_conversation
from reclaim.providers.deepseek import record_to_chat
from reclaim.providers.kept_vault import parse_vault_file
from reclaim.exporters.haevn_md import chat_to_haevn_md, export_zip

FIX = Path(__file__).resolve().parent / 'fixtures'


class TestAistudioParse(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.parsed = parse_rpc(json.loads((FIX / 'aistudio_sample.json').read_text()))

    def test_title(self):
        self.assertEqual(self.parsed['title'], 'Synthetic AI Studio Chat')

    def test_entries_and_roles(self):
        roles = [e['role'] for e in self.parsed['entries']]
        self.assertEqual(roles, ['user', 'model', 'model', 'model', 'model', 'user'])

    def test_thought_flag_structural(self):
        thoughts = [e['thought'] for e in self.parsed['entries']]
        self.assertEqual(thoughts, [False, True, False, False, False, False])

    def test_image_decoded(self):
        imgs = [e for e in self.parsed['entries'] if e['images']]
        self.assertEqual(len(imgs), 1)
        self.assertTrue(imgs[0]['images'][0].startswith(b'\x89PNG'))

    def test_attachment_ids(self):
        atts = [e for e in self.parsed['entries'] if e['attachments']]
        self.assertEqual(len(atts), 1)
        self.assertEqual(atts[0]['attachments'][0], '1AbCdEfGhIjKlMnOpQrStUvWxYz12345')

    def test_empty_groups_skipped(self):
        # group 1 has a single empty user turn -> must not appear
        self.assertEqual(len(self.parsed['entries']), 6)

    def test_entries_to_chat_without_drive(self):
        chat = entries_to_chat(self.parsed, 'chat-id-1', 'https://x')
        self.assertEqual(chat.id, 'chat-id-1')
        self.assertTrue(chat.had_thoughts)
        self.assertEqual(len(chat.visible_turns()), 5)
        att = chat.turns[-1].attachments[0]
        self.assertIsNone(att.data)  # no drive client -> link only
        self.assertIn('drive.google.com', att.source_url)


class TestAistudioPromptFile(unittest.TestCase):
    chat: ClassVar[Chat]

    @classmethod
    def setUpClass(cls):
        chat = parse_prompt_file(
            json.loads((FIX / 'aistudio_prompt_file.json').read_text()),
            chat_id='synthetic-drive-id', title='Prompt File Chat')
        assert chat is not None
        cls.chat = chat

    def test_parsed(self):
        self.assertIsNotNone(self.chat)
        self.assertEqual(self.chat.title, 'Prompt File Chat')

    def test_thoughts_from_is_thought(self):
        self.assertTrue(self.chat.had_thoughts)
        vis = self.chat.visible_turns()
        self.assertEqual(len(vis), 3)  # thought chunk excluded

    def test_image_from_inline_data(self):
        imgs = [t for t in self.chat.turns if t.images]
        self.assertEqual(len(imgs), 1)
        self.assertTrue(imgs[0].images[0].startswith(b'\x89PNG'))

    def test_attachment_from_file_data(self):
        atts = self.chat.turns[0].attachments
        self.assertEqual(len(atts), 1)
        self.assertEqual(atts[0].filename, 'notes.pdf')
        self.assertIn('drive.google.com', atts[0].source_url)

    def test_non_prompt_json_rejected(self):
        self.assertIsNone(parse_prompt_file({'hello': 'world'}))


class TestDeepseekRecord(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.chat = record_to_chat(json.loads((FIX / 'deepseek_sample.json').read_text()))

    def test_title_and_url(self):
        self.assertEqual(self.chat.title, 'Synthetic DeepSeek Chat')
        self.assertIn('synthetic-chat-id-0001', self.chat.source_url)

    def test_think_excluded(self):
        self.assertEqual(len(self.chat.turns), 2)
        self.assertNotIn('citation', self.chat.turns[0].text)

    def test_latex_preserved(self):
        self.assertIn('$P(A|B) = \\frac{P(B|A)P(A)}{P(B)}$', self.chat.turns[1].text)

    def test_citation_mapped(self):
        self.assertIn('[-1](https://example.com/bayes)', self.chat.turns[1].text)
        self.assertNotIn('[citation:1]', self.chat.turns[1].text)

    def test_file_fragments_become_attachments(self):
        atts = self.chat.turns[0].attachments
        self.assertEqual(len(atts), 2)
        self.assertEqual(atts[0].filename, 'bayes_notes.pdf')
        self.assertEqual(atts[0].kind, 'document')
        self.assertEqual(atts[1].filename, 'chart.png')
        self.assertEqual(atts[1].kind, 'image')
        self.assertTrue(atts[0].source_url.startswith(
            'https://chat.deepseek.com/file?file_id='))
        self.assertIsNone(atts[0].data)  # downloaded later by materialize_attachments


class TestKeptVault(unittest.TestCase):
    chat: ClassVar[Chat]

    @classmethod
    def setUpClass(cls):
        chat = parse_vault_file(FIX / 'kept_vault' / 'kimi' /
                                '2026-01-01_synthetic-kimi-chat.md')
        assert chat is not None
        cls.chat = chat

    def test_frontmatter(self):
        self.assertIsNotNone(self.chat)
        self.assertEqual(self.chat.id, 'kimi-synthetic-0001')
        self.assertEqual(self.chat.title, 'Synthetic Kimi Chat')
        self.assertEqual(self.chat.provider, 'kept/kimi')

    def test_roles_and_thinking_stripped(self):
        self.assertEqual([t.role for t in self.chat.turns], ['user', 'model'])
        body = self.chat.turns[1].text
        self.assertNotIn('kept:thinking', body)
        self.assertNotIn('kept:tools', body)
        self.assertNotIn('The user asks about', body)
        self.assertNotIn('search(q=', body)

    def test_latex_and_datauri_image(self):
        model_turn = self.chat.turns[1]
        self.assertIn('$e \\approx 2.71828$', model_turn.text)
        self.assertEqual(len(model_turn.images), 1)
        self.assertTrue(model_turn.images[0].startswith(b'\x89PNG'))
        self.assertNotIn('data:image', model_turn.text)  # link stripped; writer re-adds

    def test_garbage_rejected(self):
        tmp = FIX / '_garbage.md'
        tmp.write_text('no frontmatter, no messages')
        try:
            self.assertIsNone(parse_vault_file(tmp))
        finally:
            tmp.unlink()


class TestChatGPTImport(unittest.TestCase):
    chat: ClassVar[Chat]

    @classmethod
    def setUpClass(cls):
        conv = json.loads((FIX / 'chatgpt_export_sample.json').read_text())[0]
        chat = parse_conversation(conv)
        assert chat is not None
        cls.chat = chat

    def test_parsed(self):
        self.assertIsNotNone(self.chat)
        self.assertEqual(self.chat.title, 'Synthetic ChatGPT Chat')
        self.assertIn('conv-synthetic-0001', self.chat.source_url)

    def test_current_branch_linearized(self):
        texts = [t.text for t in self.chat.turns]
        self.assertIn('current branch reply', texts[-1])
        self.assertNotIn('old branch reply (not current)', ' '.join(texts))

    def test_hidden_system_and_thoughts_skipped(self):
        all_text = ' '.join(t.text for t in self.chat.turns)
        self.assertNotIn('system prompt', all_text)
        self.assertNotIn('planning how to draw a cat', all_text)

    def test_asset_pointer_becomes_note(self):
        model_turns = [t for t in self.chat.turns if t.role == 'model']
        self.assertEqual(len(model_turns), 1)
        self.assertIn('[image: not included in official export]',
                      model_turns[0].text)
        self.assertIn('$e \\approx 2.71828$', model_turns[0].text)


class TestWriter(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_full_write(self):
        chat = Chat(id='x1', title='Test: Chat/Name?', source_url='https://x',
                    turns=[
                        Turn('user', 'hello'),
                        Turn('model', 'thinking...', thought=True),
                        Turn('model', 'answer part 1', images=[b'\x89PNGfake']),
                        Turn('model', 'answer part 2'),
                        Turn('user', 'with files', attachments=[
                            Attachment('report.pdf', data=b'%PDF-fake'),
                            Attachment('report.pdf', data=b'%PDF-fake2'),  # name collision
                            Attachment('lost.txt', data=None,
                                       source_url='https://drive.example/f'),
                        ]),
                    ], provider='test')
        stats = write_chat(chat, self.tmp)
        slug = slugify(chat.title)
        md = (self.tmp / slug / f'{slug}.md').read_text()

        self.assertEqual(slug, 'Test ChatName')
        self.assertNotIn('thinking...', md)                 # thought filtered
        self.assertEqual(md.count('## Assistant'), 1)       # merged model sections
        self.assertIn('![image_1.png](image_1.png)', md)
        self.assertIn('[report.pdf](report.pdf)', md)
        self.assertIn('[report_1.pdf](report_1.pdf)', md)   # dedup
        self.assertIn('**Attachment (not downloaded):** [lost.txt]', md)
        self.assertIn('Model reasoning (thinking) turns were omitted', md)
        self.assertTrue((self.tmp / slug / 'report.pdf').exists())
        self.assertTrue((self.tmp / slug / 'report_1.pdf').exists())
        self.assertTrue((self.tmp / slug / 'image_1.png').exists())
        self.assertEqual(stats['images'], 1)
        self.assertEqual(stats['docs'], 2)
        self.assertEqual(stats['links'], 1)

    def test_img_ext(self):
        self.assertEqual(img_ext(b'\x89PNG\r\n'), '.png')
        self.assertEqual(img_ext(b'\xff\xd8\xff\xe0'), '.jpg')
        self.assertEqual(img_ext(b'GIF89a'), '.gif')
        self.assertEqual(img_ext(b'???'), '.bin')

    def test_error_and_thought_notes(self):
        chat = Chat(id='x2', title='Notes', source_url='https://x',
                    turns=[Turn('user', 'q'),
                           Turn('model', 'thoughts', thought=True,
                                error='An internal error has occurred.')],
                    provider='test')
        write_chat(chat, self.tmp)
        md = (self.tmp / 'Notes' / 'Notes.md').read_text()
        self.assertIn('API-side error', md)
        self.assertIn('thinking', md)

    def test_chat_json_dump(self):
        chat = Chat(id='x3', title='Json Dump', source_url='https://x',
                    turns=[Turn('user', 'hi'),
                           Turn('model', 'yo', images=[b'\x89PNGfake'],
                                attachments=[Attachment('a.pdf', data=b'%PDF'),
                                             Attachment('b.txt', data=None,
                                                        source_url='https://u')]),
                           Turn('model', 'think', thought=True)],
                    provider='test')
        write_chat(chat, self.tmp)
        payload = json.loads((self.tmp / 'Json Dump' / 'chat.json').read_text())
        self.assertEqual(payload['id'], 'x3')
        self.assertEqual(payload['provider'], 'test')
        self.assertTrue(payload['had_thoughts'])
        self.assertEqual(len(payload['turns']), 2)  # thoughts excluded
        self.assertEqual(payload['turns'][1]['images'], ['image_1.png'])
        atts = payload['turns'][1]['attachments']
        self.assertEqual(atts[0], {'filename': 'a.pdf', 'kind': 'document',
                                   'saved': True})
        self.assertFalse(atts[1]['saved'])
        self.assertEqual(atts[1]['source_url'], 'https://u')


if __name__ == '__main__':
    unittest.main()


class TestHaevnExport(unittest.TestCase):
    def test_haevn_md_shape(self):
        payload = {
            'id': 'abc', 'title': 'T "Quoted"', 'provider': 'aistudio',
            'turns': [
                {'role': 'user', 'text': 'question', 'images': [], 'attachments': []},
                {'role': 'model', 'text': 'answer', 'images': ['image_1.png'],
                 'attachments': [{'filename': 'doc.pdf', 'kind': 'document',
                                  'saved': True},
                                 {'filename': 'lost.txt', 'kind': 'document',
                                  'saved': False, 'source_url': 'https://u',
                                  'description': 'lost.txt'}]},
            ],
        }
        md = chat_to_haevn_md(payload)
        self.assertIn('title: "T \\"Quoted\\""', md)
        self.assertIn('source: "AI Studio"', md)
        self.assertIn('conversation_id: "abc"', md)
        self.assertIn('<!-- HAEVN: role="user" -->', md)
        self.assertIn('<!-- HAEVN: role="assistant" -->', md)
        self.assertIn('![image_1.png](image_1.png)', md)
        self.assertIn('**Attachment:** [doc.pdf](doc.pdf)', md)
        self.assertIn('**Attachment (not downloaded):** [lost.txt](https://u)', md)

    def test_export_zip_from_written_chats(self):
        import tempfile
        root = Path(tempfile.mkdtemp())
        try:
            chat = Chat(id='z1', title='Zed', source_url='https://x',
                        turns=[Turn('user', 'q'),
                               Turn('model', 'a', images=[b'\x89PNGfake'])],
                        provider='aistudio')
            write_chat(chat, root / 'Google AI Studio')
            out = root / 'export.zip'
            stats = export_zip(root, out)
            self.assertEqual(stats['chats'], 1)
            self.assertEqual(stats['media'], 1)
            import zipfile
            names = zipfile.ZipFile(out).namelist()
            self.assertIn('Google AI Studio/Zed.md', names)
            self.assertIn('Google AI Studio/Zed/image_1.png', names)
        finally:
            shutil.rmtree(root, ignore_errors=True)


class TestKimiProvider(unittest.TestCase):
    chat: ClassVar[Chat]

    @classmethod
    def setUpClass(cls):
        import reclaim.providers.kimi as kimi
        data = json.loads((FIX / 'kimi_messages_sample.json').read_text())
        msgs = list(reversed(data['messages']))  # provider reverses newest-first
        cls.chat, _ = kimi.parse_chat({'id': 'k1', 'name': 'Kimi Test'}, msgs)

    def test_chronological_order(self):
        self.assertEqual(self.chat.turns[0].role, 'user')
        self.assertIn('What is e?', self.chat.turns[0].text)

    def test_blocks_and_think_flag(self):
        think_turns = [t for t in self.chat.turns if t.thought]
        self.assertEqual(len(think_turns), 1)
        self.assertIn('Here is the answer:', think_turns[0].text)

    def test_text_content(self):
        vis = self.chat.visible_turns()
        self.assertIn('$e \\approx 2.718$', vis[-1].text)


class TestChatGPTProvider(unittest.TestCase):

    def test_list_projects_pagination(self):
        import reclaim.providers.chatgpt as cg
        pages = [
            {'items': [{'gizmo': {'gizmo': {'id': 'g-p-aaa',
                                            'display': {'name': 'Alpha'}}}}],
             'cursor': 2},
            {'items': [{'gizmo': {'gizmo': {'id': 'g-p-bbb',
                                            'display': {'name': 'Beta'}}}}],
             'cursor': None},
        ]
        calls = iter(pages)
        orig = cg._get
        cg._get = lambda page, token, url: next(calls)
        try:
            projs = cg.list_projects(None, 'tok')
        finally:
            cg._get = orig
        self.assertEqual(projs, [{'id': 'g-p-aaa', 'name': 'Alpha'},
                                 {'id': 'g-p-bbb', 'name': 'Beta'}])

    def test_list_all_conversations_dedup_and_project_tag(self):
        import reclaim.providers.chatgpt as cg
        orig = cg.list_conversations, cg.list_projects,             cg.list_project_conversations
        cg.list_conversations = lambda page, tok: [
            {'id': 'c1', 'title': 'Main'},   # dup of project -> skipped
            {'id': 'c3', 'title': 'Unfiled'}]
        cg.list_projects = lambda page, tok: [{'id': 'g-p-x', 'name': 'Proj'}]
        cg.list_project_conversations = lambda page, tok, gid: [
            {'id': 'c1', 'title': 'Main'},  # also in main -> project tag wins
            {'id': 'c2', 'title': 'InProject'}]
        try:
            items = cg.list_all_conversations(None, 'tok')
        finally:
            (cg.list_conversations, cg.list_projects,
             cg.list_project_conversations) = orig
        # projects are processed FIRST: c1 (also in main) keeps project tag
        self.assertEqual([c['id'] for c in items], ['c1', 'c2', 'c3'])
        self.assertEqual(items[0]['_project'], 'Proj')
        self.assertEqual(items[1]['_project'], 'Proj')
        self.assertIsNone(items[2]['_project'])

    def test_node_asset_collection(self):
        import reclaim.providers.chatgpt as cg
        data = json.loads((FIX / 'chatgpt_api_sample.json').read_text())
        nodes = cg._linearize(data['mapping'], data['current_node'])
        self.assertEqual(len(nodes), 2)
        turn, assets = cg._node_to_turn(nodes[1])
        assert turn is not None
        self.assertEqual(assets, ['file-XYZ123'])
        self.assertIn('Here it is:', turn.text)
        self.assertIn('As you can see...', turn.text)

    def test_asset_regex_variants(self):
        import reclaim.providers.chatgpt as cg
        m1 = cg._ASSET_RE.search('file-service://file-ABC123')
        assert m1 is not None
        self.assertEqual(m1.group(1), 'file-ABC123')
        m2 = cg._ASSET_RE.search('sediment://file_ABC123')
        assert m2 is not None
        self.assertEqual(m2.group(1), 'file_ABC123')
