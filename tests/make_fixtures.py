"""Generate synthetic test fixtures (no private data).

Run once: python3.14 tests/make_fixtures.py
Fixtures document the raw provider formats our parsers consume.
"""

import base64
import json
import struct
import zlib
from pathlib import Path

FIX = Path(__file__).resolve().parent / 'fixtures'
FIX.mkdir(exist_ok=True)


def make_png_b64(size: int = 64) -> str:
    """Build a valid solid-color PNG (stdlib only) big enough (>500 chars b64)
    to pass the parser's inline-image threshold."""
    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (struct.pack('>I', len(payload)) + tag + payload +
                struct.pack('>I', zlib.crc32(tag + payload) & 0xffffffff))
    ihdr = struct.pack('>IIBBBBB', size, size, 8, 2, 0, 0, 0)  # 8-bit RGB
    # pseudo-random pixels (LCG) so IDAT doesn't compress below the
    # parser's 500-char inline-image threshold
    seed = 42
    def rnd() -> int:
        nonlocal seed
        seed = (seed * 1103515245 + 12345) & 0x7fffffff
        return seed % 256
    row = b'\x00' + bytes(rnd() for _ in range(size * 3))
    idat = zlib.compress(row * size)
    png = (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', ihdr) +
           chunk(b'IDAT', idat) + chunk(b'IEND', b''))
    return base64.b64encode(png).decode()


PNG_B64 = make_png_b64()


def turn(role, text='', thought=False, image=False, drive_ids=None, error=None):
    t: list = [None] * 36
    t[0] = text
    t[8] = role
    if drive_ids:
        t[1] = drive_ids
    if image:
        t[12] = ['image/png', PNG_B64]
    if thought:
        t[19] = 1
    if error:
        t[28] = error
    t[28] = t[28] or ''
    t[30] = 0
    t[31] = 0
    t[35] = ''
    return t


aistudio = [[
    None, None, None, None,
    ['Synthetic AI Studio Chat', None, ['Test User', 1, ''], None, [['1700000000', 0]]],
    None, None, None, None, None, None, None, None,
    [  # inner[13]: turn groups
      [
        turn('user', 'What is the Josephus problem?'),
        turn('model', '**Analyzing the Problem**\n\nI am thinking about the Josephus problem...', thought=True),
        turn('model', 'The **Josephus problem** is a counting-out game.\n\n$$f(n,k) = (f(n-1,k) + k) \\bmod n$$'),
        turn('model', '', image=True),
        turn('model', 'The formula above gives the safe position (0-indexed).'),
        turn('user', '', drive_ids=['1AbCdEfGhIjKlMnOpQrStUvWxYz12345']),
      ],
      [turn('user', '')],  # empty draft group -> skipped
    ],
]]

deepseek = {
    'data': {
        'chat_session': {'id': 'synthetic-chat-id-0001',
                         'title': 'Synthetic DeepSeek Chat',
                         'updated_at': 1700000000},
        'chat_messages': [
            {'role': 'USER', 'fragments': [
                {'type': 'REQUEST', 'content': 'Explain Bayes theorem with a source.'},
                {'type': 'FILE', 'files': [
                    {'id': 'file-synthetic-1', 'status': 'SUCCESS',
                     'file_name': 'bayes_notes.pdf', 'file_size': 12345,
                     'signed_path': '/file?file_id=file-synthetic-1&state=signedtoken123'},
                    {'id': 'file-synthetic-2', 'status': 'SUCCESS',
                     'file_name': 'chart.png', 'file_size': 54321,
                     'signed_path': '/file?file_id=file-synthetic-2&state=signedtoken456'}]}]},
            {'role': 'ASSISTANT', 'fragments': [
                {'type': 'SEARCH', 'results': [
                    {'cite_index': 1, 'url': 'https://example.com/bayes',
                     'title': 'Bayes theorem', 'snippet': '...'}]},
                {'type': 'THINK', 'content': 'The user wants an explanation with citation.'},
                {'type': 'RESPONSE',
                 'content': 'Bayes theorem: $P(A|B) = \\frac{P(B|A)P(A)}{P(B)}$ [citation:1]'}]},
        ],
    }
}

(FIX / 'aistudio_sample.json').write_text(json.dumps(aistudio))
(FIX / 'deepseek_sample.json').write_text(json.dumps(deepseek))

# makersuite.prompt Drive-file format (offline parse mode)
prompt_file = {
    'runSettings': {'model': 'models/gemini-test', 'temperature': 1.0},
    'systemInstruction': {},
    'chunkedPrompt': {
        'chunks': [
            {'text': 'Upload question', 'role': 'user', 'tokenCount': 3,
             'createTime': '2026-01-01T00:00:00Z',
             'parts': [{'fileData': {'fileId': '1DriveFileIdSynthetic000000000',
                                     'displayName': 'notes.pdf'}}]},
            {'text': '**Thinking aloud**\n\nI consider the question...',
             'role': 'model', 'isThought': True, 'tokenCount': 9},
            {'text': 'Here is the answer with an image:',
             'role': 'model', 'tokenCount': 7,
             'parts': [{'text': 'Here is the answer with an image:'},
                       {'inlineData': {'mimeType': 'image/png', 'data': PNG_B64}}]},
            {'text': 'And the follow-up explanation.',
             'role': 'model', 'tokenCount': 5, 'finishReason': 'STOP'},
        ],
        'pendingInputs': [{'text': '', 'role': 'user'}],
    },
}
(FIX / 'aistudio_prompt_file.json').write_text(json.dumps(prompt_file))

# synthetic Kept vault (import mode)
kept_md = f'''---
id: "kimi-synthetic-0001"
platform: "kimi"
title: "Synthetic Kimi Chat"
synced: 2026-01-02T10:00:00+00:00
created_at: 2026-01-01T09:00:00+00:00
updated_at: 2026-01-01T09:30:00+00:00
messages: 2
model: "kimi-k2"
tags:
  - "kept/kimi"
---

# Synthetic Kimi Chat

### You — 2026-01-01 09:00

What is $e$ and show me a plot?

---

### Assistant — 2026-01-01 09:01

<!-- kept:thinking -->
The user asks about e and wants a plot.
<!-- /kept:thinking -->

<!-- kept:tools -->
- search(q="euler number")
<!-- /kept:tools -->

$e \\approx 2.71828$ is Euler's number. Here is the plot:

![plot](data:image/png;base64,{PNG_B64})

---
'''
kept_dir = FIX / 'kept_vault' / 'kimi'
kept_dir.mkdir(parents=True, exist_ok=True)
(kept_dir / '2026-01-01_synthetic-kimi-chat.md').write_text(kept_md)

# synthetic ChatGPT official export (branching + hidden + asset pointer)
chatgpt_export = [{
    'id': 'conv-synthetic-0001',
    'title': 'Synthetic ChatGPT Chat',
    'create_time': 1700000000.0,
    'current_node': 'n6',
    'mapping': {
        'n1': {'id': 'n1', 'parent': None, 'children': ['n2'],
               'message': {'id': 'm1', 'author': {'role': 'system'},
                           'content': {'content_type': 'text', 'parts': ['system prompt']},
                           'metadata': {'is_visually_hidden_from_conversation': True}}},
        'n2': {'id': 'n2', 'parent': 'n1', 'children': ['n3'],
               'message': {'id': 'm2', 'author': {'role': 'user'},
                           'content': {'content_type': 'text',
                                       'parts': ['Draw a cat and explain $e$.']},
                           'metadata': {}}},
        'n3': {'id': 'n3', 'parent': 'n2', 'children': ['n4'],
               'message': {'id': 'm3', 'author': {'role': 'assistant'},
                           'content': {'content_type': 'thoughts',
                                       'parts': ['planning how to draw a cat']},
                           'metadata': {}}},
        'n4': {'id': 'n4', 'parent': 'n3', 'children': ['n5', 'n6'],
               'message': {'id': 'm4', 'author': {'role': 'assistant'},
                           'content': {'content_type': 'multimodal_text',
                                       'parts': ['Here is your cat:',
                                                  {'content_type': 'image_asset_pointer',
                                                   'asset_pointer': 'file-service://file-ABC'},
                                                  'And $e \\approx 2.71828$.']},
                           'metadata': {}}},
        'n5': {'id': 'n5', 'parent': 'n4', 'children': [],
               'message': {'id': 'm5', 'author': {'role': 'user'},
                           'content': {'content_type': 'text',
                                       'parts': ['old branch reply (not current)']},
                           'metadata': {}}},
        'n6': {'id': 'n6', 'parent': 'n4', 'children': [],
               'message': {'id': 'm6', 'author': {'role': 'user'},
                           'content': {'content_type': 'text',
                                       'parts': ['current branch reply']},
                           'metadata': {}}},
    },
}]
(FIX / 'chatgpt_export_sample.json').write_text(json.dumps(chatgpt_export))
print('fixtures written to', FIX)

# synthetic Kimi API shapes (scrape mode)
kimi_messages = {'messages': [  # newest-first as the API returns
    {'role': 'assistant', 'blocks': [
        {'type': 'TEXT', 'text': {'content': 'Euler number is $e \\approx 2.718$.'}}],
     'create_time': 1700000100},
    {'role': 'assistant', 'blocks': [
        {'type': 'THINK', 'text': {'content': 'reasoning about e...'}},
        {'type': 'TEXT', 'text': {'content': 'Here is the answer:'}}],
     'create_time': 1700000050},
    {'role': 'user', 'content': 'What is e?', 'create_time': 1700000000},
]}
(FIX / 'kimi_messages_sample.json').write_text(json.dumps(kimi_messages))

# synthetic ChatGPT backend-api conversation (asset pointer variant)
chatgpt_api_conv = {
    'title': 'API Chat With Asset',
    'current_node': 'n2',
    'mapping': {
        'n1': {'id': 'n1', 'parent': None, 'children': ['n2'],
               'message': {'id': 'm1', 'author': {'role': 'user'},
                           'content': {'content_type': 'text',
                                       'parts': ['make a chart']},
                           'metadata': {}}},
        'n2': {'id': 'n2', 'parent': 'n1', 'children': [],
               'message': {'id': 'm2', 'author': {'role': 'assistant'},
                           'content': {'content_type': 'multimodal_text',
                                       'parts': ['Here it is:',
                                                  {'content_type': 'image_asset_pointer',
                                                   'asset_pointer': 'file-service://file-XYZ123'},
                                                  'As you can see...']},
                           'metadata': {}}},
    },
}
(FIX / 'chatgpt_api_sample.json').write_text(json.dumps(chatgpt_api_conv))
print('fixtures written to', FIX)
