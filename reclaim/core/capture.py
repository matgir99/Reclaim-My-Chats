"""Captures: a provider's real responses with the chats' text replaced, for
bug reports and test fixtures (`reclaim capture`, Diagnostics in the window).

A capture keeps what the parsers need to read it the way they read the
original:

- the structure: objects, lists, field names, numbers, true/false/null;
- numbers and dates written as text (timestamps, sizes, counts);
- strings that occur word for word in the provider code (role names,
  content types, RPC names), and short codes in fields that name a type,
  role, status or format;
- inside links, paths, ids and markers: the words the parsers look for
  (https, file, citation...), numbers of up to three digits (the 1 of
  [citation:1]), and every punctuation mark.

Everything else is replaced: each letter by a random letter of the same
case, each digit by a random digit. Text keeps its length, spacing and
punctuation, so Markdown and citation markers survive. Equal strings get
equal replacements and different strings different ones, so ids still match
up; a string that also occurs inside a longer one (an id inside a link, a
citation marker inside a reply) is replaced the same way there. Pictures
become one small generated picture.

The replacement letters are random, not derived from the originals, so
they cannot be turned back into the text.
"""

from __future__ import annotations

import base64
import json
import random
import re
import secrets
import struct
import traceback
import types
import zlib
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from functools import cache
from pathlib import Path
from typing import Any

from . import batchexecute
from .fsutil import atomic_write_text

FORMAT = 'reclaim-capture'
VERSION = 1
ABOUT = ('Responses recorded while fetching one chat. Every letter and digit of '
         'the chat is replaced; field names, numbers, dates and the words the '
         'parsers look for are kept. Look through it before sharing it.')

_WORD = re.compile(r'[^\W_]+')         # letters and digits, in any script
_SPACE = re.compile(r'(\s+)')
_NUMBER = re.compile(r'-?\d+(?:\.\d+)?')
# Inside a marker or link: an index such as the 1 of [citation:1]. Longer
# numbers there can be account or order numbers, and are replaced.
_SHORT_NUMBER = re.compile(r'\d{1,3}')
_DATE = re.compile(r'\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?'
                   r'(?:Z|[+-]\d{2}:?\d{2})?)?')
_FIELD = re.compile(r'[A-Za-z_][A-Za-z0-9_]{0,39}')
_EXTENSION = re.compile(r'\.([A-Za-z0-9]{2,4})$')
# Extensions the parsers check (an uploaded image or document).
_FILE_TYPES = frozenset({'png', 'jpg', 'jpeg', 'gif', 'webp', 'svg', 'pdf', 'txt', 'csv',
                         'json', 'md', 'zip', 'html', 'xml', 'docx', 'xlsx', 'pptx'})
_BASE64 = re.compile(r'[A-Za-z0-9+/_-]{96,}={0,2}')
_DATA_URI = re.compile(r'(data:image/[\w.+-]+;base64,)[A-Za-z0-9+/=]+')
_IMAGE_MAGIC = (b'\x89PNG', b'\xff\xd8\xff', b'GIF8', b'RIFF')
# Prose punctuation is not structure. These characters mark a link, a path,
# a marker or code inside text, where the parsers' words are kept.
_STRUCTURE = frozenset('[]{}<>/\\:=_|@#`')
_PRIVATE_USE = ('\ue000', '\uf8ff')    # ChatGPT's citation markers use these
# Fields whose short values are codes rather than text.
_CODE_FIELDS = frozenset({'type', 'role', 'kind', 'status', 'state', 'format', 'recipient',
                          'model', 'model_slug', 'content_type', 'mime_type', 'mimetype',
                          'media_type', 'finish_reason', 'stop_reason'})
# A string this long or longer counts when it occurs inside another one
# (ChatGPT's shortest citation markers have five characters).
SHARED_MIN = 3


# ---------------------------------------------------------------------------
# What is kept: the provider code's own strings
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Vocabulary:
    values: frozenset[str]   # a whole string equal to one of these is kept
    words: frozenset[str]    # kept inside links, paths, ids and markers
    label_words: frozenset[str]  # kept in request labels and error messages


def _parts(obj, module: str) -> Iterable:
    """What a module-level value holds that may contain strings."""
    if isinstance(obj, re.Pattern):
        return (obj.pattern,)
    if isinstance(obj, (list, tuple, set, frozenset)):
        return obj
    if isinstance(obj, dict):
        return (*obj.keys(), *obj.values())
    if isinstance(obj, types.CodeType):
        return obj.co_consts
    if isinstance(obj, types.FunctionType):
        return (obj.__code__,) if obj.__module__ == module else ()
    if isinstance(obj, (staticmethod, classmethod)):
        return (obj.__func__,)
    if isinstance(obj, property):
        return (obj.fget,)
    if isinstance(obj, type) and obj.__module__ == module:
        return tuple(vars(obj).values())
    return ()


def _code_strings(module) -> set[str]:
    """Every string constant of a module: its variables and the constants in
    the code of its functions and classes. Read from the loaded code, so it
    also works in the desktop app, which ships no source files."""
    found: set[str] = set()
    seen: set[int] = set()
    # Not __file__ and the like: those are this computer's paths.
    pending = [value for key, value in vars(module).items() if not key.startswith('__')]
    while pending:
        obj = pending.pop()
        if isinstance(obj, str):
            found.add(obj)
        elif id(obj) not in seen:
            seen.add(id(obj))
            pending.extend(_parts(obj, module.__name__))
    return found


@cache
def vocabulary() -> Vocabulary:
    """The strings the provider code uses, which a capture keeps."""
    from ..providers import (aistudio_files, chatgpt, chatgpt_import, chatgpt_references,
                             claude, deepseek, googleaistudio, googlegemini, kimi)
    strings: set[str] = set()
    for module in (batchexecute, aistudio_files, chatgpt, chatgpt_import, chatgpt_references,
                   claude, deepseek, googleaistudio, googlegemini, kimi):
        strings |= _code_strings(module)
    return vocabulary_of(strings)


def vocabulary_of(strings: Iterable[str]) -> Vocabulary:
    # A string holding the home folder was computed on this computer, not
    # written in the code; its words (the user name) must not be kept.
    home = str(Path.home())
    strings = {s for s in strings if home not in s}
    compact = [s for s in strings if s and len(s) <= 100 and not any(c.isspace() for c in s)]
    return Vocabulary(
        values=frozenset(strings),
        words=frozenset(w for s in compact for w in _WORD.findall(s) if len(w) >= 2),
        label_words=frozenset(w for s in strings for w in _WORD.findall(s) if len(w) >= 2))


# ---------------------------------------------------------------------------
# Replacing the text
# ---------------------------------------------------------------------------
@cache
def sample_png() -> str:
    """A small valid PNG, base64: what every picture becomes. Its noise keeps
    it longer than the size below which parsers ignore inline images."""
    size, seed = 16, 42

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (struct.pack('>I', len(payload)) + tag + payload
                + struct.pack('>I', zlib.crc32(tag + payload) & 0xffffffff))

    noise = bytearray()
    for _ in range(size * size * 3):
        seed = (seed * 1103515245 + 12345) & 0x7fffffff
        noise.append(seed % 256)
    rows = b''.join(b'\x00' + bytes(noise[r * size * 3:(r + 1) * size * 3]) for r in range(size))
    png = (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', size, size, 8, 2, 0, 0, 0))
           + chunk(b'IDAT', zlib.compress(rows)) + chunk(b'IEND', b''))
    return base64.b64encode(png).decode()


def _embedded_json(text: str):
    """The object or list a string holds as JSON (Google nests JSON in
    strings), else None."""
    if len(text) < 2 or text[0] not in '[{':
        return None
    try:
        value = json.loads(text)
    except ValueError:
        return None
    return value if isinstance(value, (list, dict)) else None


def _is_picture(text: str) -> bool:
    """True for a base64 image (PNG, JPEG, GIF or WebP)."""
    if not _BASE64.fullmatch(text):
        return False
    try:
        head = base64.b64decode(text[:16].replace('-', '+').replace('_', '/'))
    except ValueError:
        return False
    return head.startswith(_IMAGE_MAGIC)


def _is_structural(chunk: str) -> bool:
    return any(c in _STRUCTURE or _PRIVATE_USE[0] <= c <= _PRIVATE_USE[1] for c in chunk)


def _code_field(key: str | None, value: str) -> bool:
    """A short code in a field that names a type, role, status or format."""
    if key is None or len(value) > 64 or any(c.isspace() for c in value):
        return False
    return key.lower() in _CODE_FIELDS or key.endswith(('_type', 'Type'))


class Sanitizer:
    """Replaces the text of JSON values (see the module docstring). Call
    `prepare` with every value of one capture before cleaning any of them,
    so ids and strings shared between them are recognised."""

    def __init__(self, vocab: Vocabulary | None = None, *, seed: int | None = None):
        self.vocab = vocab if vocab is not None else vocabulary()
        self._random = random.Random(secrets.randbits(64) if seed is None else seed)
        self._done: dict[str, str] = {}
        self._used: set[str] = set()
        self._values: set[str] = set()
        self._shared: dict[str, list[str]] = {}  # first characters -> strings

    def prepare(self, values: Iterable[Any]) -> None:
        for value in values:
            self._collect(value)
        shared: dict[str, list[str]] = {}
        for text in self._values:
            if self._shareable(text):
                shared.setdefault(text[:SHARED_MIN], []).append(text)
        for candidates in shared.values():
            candidates.sort(key=len, reverse=True)  # the longest match wins
        self._shared = shared

    def _collect(self, value) -> None:
        if isinstance(value, str):
            inner = _embedded_json(value)
            if inner is None:
                self._values.add(value)
            else:
                self._collect(inner)
        elif isinstance(value, dict):
            for item in value.values():
                self._collect(item)
        elif isinstance(value, list):
            for item in value:
                self._collect(item)

    def clean(self, value, key: str | None = None):
        """`value` with its text replaced; everything else as it was."""
        if isinstance(value, dict):
            return {self._key(k): self.clean(v, k) for k, v in value.items()}
        if isinstance(value, list):
            return [self.clean(item) for item in value]
        if isinstance(value, str):
            return value if _code_field(key, value) else self.text(value)
        return value  # numbers, true, false, null

    def _key(self, key: str) -> str:
        """Field names stay: those the provider code reads, and names shaped
        like an API's (`content_type`, `chunkedPrompt`). A key that is an id
        (it also occurs as a value) or a word such as a name is replaced."""
        if key in self.vocab.values or (_FIELD.fullmatch(key) and not key[0].isupper()
                                        and key not in self._values):
            return key
        return self.text(key)

    def text(self, text: str) -> str:
        """One string with its text replaced; the same string always gives the
        same result, and different strings different results."""
        done = self._done.get(text)
        if done is not None:
            return done
        result = self._replace(text)
        for _ in range(20):  # a random replacement that equals another one
            if result == text or result not in self._used:
                break
            result = self._replace(text)
        self._done[text] = result
        self._used.add(result)
        return result

    def label(self, text: str) -> str:
        """Our own wording, such as a request's label or an error message:
        its words stay, ids and response text inside it are replaced."""
        return self._rewrite(text, self.vocab.label_words, everywhere=True)

    def _kept(self, text: str) -> bool:
        return (not text or text in self.vocab.values
                or bool(_NUMBER.fullmatch(text) or _DATE.fullmatch(text)))

    def _replace(self, text: str) -> str:
        if self._kept(text):
            return text
        inner = _embedded_json(text)
        if inner is not None:
            return json.dumps(self.clean(inner), ensure_ascii=False, separators=(',', ':'))
        uri = _DATA_URI.fullmatch(text)
        if uri:
            return uri.group(1) + sample_png()
        if _is_picture(text):
            return sample_png()
        # Without spaces, the whole string is an id, a link or a code.
        return self._rewrite(text, self.vocab.words,
                             everywhere=not any(c.isspace() for c in text))

    def _rewrite(self, text: str, words: frozenset[str], *, everywhere: bool) -> str:
        """Replace the parts of `text` outside shared strings, which get their
        own (consistent) replacement."""
        parts: list[str] = []
        start = i = 0
        while i < len(text):
            shared = self._shared_at(text, i)
            if shared is None:
                i += 1
                continue
            parts.append(self._mask(text[start:i], words, everywhere))
            parts.append(self.text(shared))
            i = start = i + len(shared)
        parts.append(self._mask(text[start:], words, everywhere))
        return ''.join(parts)

    def _shareable(self, text: str) -> bool:
        """Whether a string is matched inside others: ids, links, markers and
        sentences, but no single plain word (a `system` role must not match
        inside "systems", nor keep the words of an id there)."""
        if len(text) < SHARED_MIN or self._kept(text):
            return False
        if _WORD.fullmatch(text):
            return len(text) >= 16 or any(c.isdigit() for c in text)
        return True

    def _shared_at(self, text: str, index: int) -> str | None:
        """The longest other string of the capture that starts at `index`,
        never in the middle of a word."""
        if index and text[index - 1].isalnum() and text[index].isalnum():
            return None
        for candidate in self._shared.get(text[index:index + SHARED_MIN], ()):
            end = index + len(candidate)
            if (len(candidate) < len(text) and text.startswith(candidate, index)
                    and not (end < len(text) and text[end].isalnum()
                             and candidate[-1].isalnum())):
                return candidate
        return None

    def _mask(self, segment: str, words: frozenset[str], everywhere: bool) -> str:
        """Letters and digits replaced, chunk by chunk (the runs between
        spaces); spaces stay as they are."""
        return ''.join(chunk if not chunk or chunk.isspace()
                       else self._mask_chunk(chunk, words, everywhere or _is_structural(chunk))
                       for chunk in _SPACE.split(segment))

    def _mask_chunk(self, chunk: str, words: frozenset[str], keep: bool) -> str:
        """One chunk. With `keep` (a link, path, id or marker) the given words
        and short numbers stay; a file extension the parsers check always
        stays."""
        ext = _EXTENSION.search(chunk)
        ext_start = ext.start(1) if ext and ext.group(1).lower() in _FILE_TYPES else -1

        def word(match: re.Match) -> str:
            found = match.group()
            if match.start() == ext_start or (
                    keep and (found in words or _SHORT_NUMBER.fullmatch(found))):
                return found
            return self._scramble(found)
        return _WORD.sub(word, chunk)

    def _scramble(self, word: str) -> str:
        choice = self._random.choice
        return ''.join(choice('0123456789') if c.isdigit()
                       else choice('ABCDEFGHIJKLMNOPQRSTUVWXYZ') if c.isupper()
                       else choice('abcdefghijklmnopqrstuvwxyz') for c in word)


# ---------------------------------------------------------------------------
# Capture files
# ---------------------------------------------------------------------------
@dataclass
class Response:
    request: str           # what was asked, in the provider code's words
    status: int | None
    text: str              # the response body, as the provider code reads it


@dataclass
class Capture:
    provider: str
    error: dict | None     # why the chat could not be read, if it could not
    responses: list[Response]


def _decode(text: str) -> tuple[str, Any]:
    """A response body as ('json', value), ('batchexecute', frames) or
    ('text', text)."""
    try:
        return 'json', json.loads(text)
    except ValueError:
        pass
    if text.lstrip().startswith(")]}'"):
        frames = batchexecute.parse_frames(text)
        try:
            return 'batchexecute', [json.loads(frame) for frame in frames]
        except ValueError:
            pass
    return 'text', text


def _encode(body_format: str, body) -> str:
    """The body back as the text the provider code reads."""
    if body_format == 'json':
        return json.dumps(body, ensure_ascii=False)
    if body_format == 'batchexecute':
        frames = [json.dumps(frame, ensure_ascii=False, separators=(',', ':')) for frame in body]
        return ")]}'\n" + ''.join(f'\n{len(f.encode("utf-16-le")) // 2}\n{f}' for f in frames)
    return body


def _where(error: BaseException) -> str:
    """The innermost line of this app's code that the error passed through."""
    frames = [f for f in traceback.extract_tb(error.__traceback__)
              if 'reclaim' in Path(f.filename).parts]
    if not frames:
        return ''
    frame = frames[-1]
    parts = Path(frame.filename).parts
    path = '/'.join(parts[len(parts) - 1 - parts[::-1].index('reclaim'):])
    return f'{path}:{frame.lineno} {frame.name}'


def _app_version() -> str:
    from importlib.metadata import PackageNotFoundError, version
    try:
        return version('reclaimmychats')
    except PackageNotFoundError:
        return 'dev'


def build(provider: str, responses: list[dict], error: BaseException | None = None,
          *, sanitizer: Sanitizer | None = None) -> dict:
    """The capture document for recorded responses (see retry.recording)."""
    sanitizer = sanitizer or Sanitizer()
    bodies = [_decode(str(r.get('text') or '')) for r in responses]
    sanitizer.prepare(body for _, body in bodies)
    return {
        'format': FORMAT,
        'version': VERSION,
        'provider': provider,
        'app_version': _app_version(),
        'captured': date.today().isoformat(),
        'about': ABOUT,
        'error': None if error is None else {
            'type': type(error).__name__,
            'message': sanitizer.label(str(error)),
            'where': _where(error)},
        'responses': [{'request': sanitizer.label(str(r.get('request') or '')),
                       'status': r.get('status'),
                       'body_format': body_format,
                       'body': sanitizer.clean(body)}
                      for r, (body_format, body) in zip(responses, bodies, strict=True)],
    }


def default_path(root: Path, provider: str) -> Path:
    """Where a capture goes unless the user names a file: `captures` in the
    data folder, next to the logs."""
    return Path(root) / 'captures' / f'{provider}-{datetime.now():%Y%m%d-%H%M%S}.json'


def save(path: Path, provider: str, responses: list[dict],
         error: BaseException | None = None) -> Path:
    path = Path(path)
    document = build(provider, responses, error)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(document, ensure_ascii=False, indent=1) + '\n')
    return path


def load(path: Path) -> Capture:
    """A capture file, with each body as the text the provider code reads:
    hand `responses[i].text` to the provider's parser in a test."""
    document = json.loads(Path(path).read_text(encoding='utf-8'))
    if document.get('format') != FORMAT:
        raise ValueError(f'{path} is not a Reclaim My Chats capture')
    return Capture(provider=document.get('provider', ''), error=document.get('error'),
                   responses=[Response(r.get('request', ''), r.get('status'),
                                       _encode(r.get('body_format', 'text'), r.get('body')))
                              for r in document.get('responses', [])])
