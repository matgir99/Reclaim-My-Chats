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
    t = [None] * 36
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
print('fixtures written to', FIX)
