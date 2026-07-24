"""Offline parser/writer tests — no browser required.

Run: python3.14 -m unittest discover tests   (or ./run_tests.sh)
"""

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from reclaim.core.model import Attachment, Chat, Turn
from reclaim.core.writer import img_ext, slugify, write_chat
from reclaim.providers.aistudio import entries_to_chat, parse_rpc
from reclaim.providers.aistudio_files import parse_prompt_file
from reclaim.providers.deepseek import record_to_chat

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
    @classmethod
    def setUpClass(cls):
        cls.chat = parse_prompt_file(
            json.loads((FIX / 'aistudio_prompt_file.json').read_text()),
            chat_id='synthetic-drive-id', title='Prompt File Chat')

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


if __name__ == '__main__':
    unittest.main()
