"""Read Google Antigravity's conversation steps (protobuf) into chat turns.

Antigravity keeps each step of a conversation as one protobuf message,
`gemini_coder.Step`, in the `steps` table of the conversation's SQLite file
(see antigravity.py). The `agy` program embeds the schema; the field numbers
below come from it (agy 1.5.19, November 2026). Protobuf field numbers stay
fixed for as long as old files must remain readable, which Antigravity itself
needs, so only the numbers used here are listed.

A step that does not parse, or a field with an unexpected type, raises
`FormatError`: the chat is then not saved, with a message saying so.
"""

from __future__ import annotations

import base64
import binascii
import json
import posixpath
import re
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urlparse

from ..core.model import Turn


class FormatError(RuntimeError):
    """A step is not in the format this version understands."""


# gemini_coder.Step
STEP_TYPE, STEP_STATUS, STEP_METADATA = 1, 4, 5
STEP_USER_INPUT, STEP_PLANNER, STEP_ERROR_MESSAGE = 19, 20, 24
STEP_CHECKPOINT, STEP_ERROR, STEP_SYSTEM_MESSAGE, STEP_GENERIC = 30, 31, 114, 140
# exa.cortex_pb.CortexStepType
USER_INPUT, PLANNER_RESPONSE, ERROR_MESSAGE = 14, 15, 17
CHECKPOINT, SYSTEM_MESSAGE, GENERIC = 23, 101, 132
TYPE_NAMES = {USER_INPUT: 'user_input', PLANNER_RESPONSE: 'planner_response',
              ERROR_MESSAGE: 'error_message', CHECKPOINT: 'checkpoint',
              SYSTEM_MESSAGE: 'system_message', GENERIC: 'tool'}
# exa.cortex_pb.CortexStepStatus
INVALID, CLEARED, CANCELED, ERROR, INTERRUPTED = 4, 5, 6, 7, 12
STATUS_NAMES = {0: 'unspecified', 1: 'pending', 2: 'running', 3: 'done', INVALID: 'invalid',
                CLEARED: 'cleared', CANCELED: 'canceled', ERROR: 'error', 8: 'generating',
                9: 'waiting', 11: 'queued', INTERRUPTED: 'interrupted'}
_STATUS_SUFFIX = {CANCELED: 'canceled', ERROR: 'failed', INTERRUPTED: 'interrupted'}
# exa.cortex_pb.CortexStepMetadata, google.protobuf.Timestamp, ChatToolCall
META_CREATED_AT, META_TOOL_CALL = 1, 4
TIME_SECONDS = 1
CALL_NAME, CALL_ARGUMENTS = 2, 3
# exa.cortex_pb.CortexStepUserInput, TextOrScopeItem, ImageData, Media
INPUT_QUERY, INPUT_TEXT, INPUT_ITEMS, INPUT_IMAGES, INPUT_MEDIA = 1, 2, 3, 5, 9
ITEM_TEXT = 1
IMAGE_BASE64, IMAGE_MIME, IMAGE_URI = 1, 2, 4
MEDIA_MIME, MEDIA_INLINE, MEDIA_URI = 1, 2, 5
# exa.cortex_pb.CortexStepPlannerResponse
PLAN_RESPONSE, PLAN_THINKING, PLAN_TOOL_CALLS, PLAN_MODIFIED, PLAN_RAW_THINKING = 1, 3, 7, 8, 16
# exa.cortex_pb.CortexStepGeneric, GenericStepResult, StepRenderInfo and its parts
GENERIC_ARGS, GENERIC_RESULT = 1, 2
ENTRY_KEY, ENTRY_VALUE = 1, 2
RESULT_TEXT, RESULT_RENDER, RESULT_MEDIA = 1, 3, 4
RENDER_TITLE, RENDER_HIDDEN, RENDER_PREFIX, RENDER_PARTS = 1, 4, 5, 9
PART_TEXT, PART_FILE, PART_FILE_DIFF, PART_AGENT = 1, 2, 4, 5
FILE_URI, DIFF_STATS = 1, 2
STATS_ADDED, STATS_DELETED = 1, 2
AGENT_CONVERSATION, AGENT_TITLE = 1, 2
# exa.cortex_pb.CortexStepErrorMessage, CortexErrorDetails
ERROR_DETAILS = 3
DETAILS_USER_MESSAGE, DETAILS_SHORT = 1, 2
# exa.cortex_pb.CortexStepCheckpoint, CortexStepSystemMessage
CHECKPOINT_TITLE, CHECKPOINT_SUMMARY = 10, 5
SYSTEM_TEXT, SYSTEM_EVENT = 1, 3

_RAW_RESULT_CHARS = 20_000
_ANSWER = re.compile(r'A\d+:')
# A message between agents: '[Message] timestamp=… sender=<id> priority=… content=<text>'.
_AGENT_MESSAGE = re.compile(r'\[Message\]([^\n]*?)\bcontent=')
# Words for tools whose step carries no display line of its own.
_TOOL_VERBS = {'view_file': 'Viewed', 'read_file': 'Analyzed', 'run_command': 'Ran',
               'search_web': 'Searched', 'edit_file': 'Edited', 'write_file': 'Created',
               'manage_task': 'Checked task', 'enter_plan_mode': 'Entered plan mode',
               'invoke_subagent': 'Started subagent', 'send_message': 'Messaged',
               'ask_question': 'Asked', 'exit_plan_mode': 'Proposed a plan'}


def _varint(buf: bytes, i: int) -> tuple[int, int]:
    value = shift = 0
    for _ in range(10):
        if i >= len(buf):
            raise FormatError('a step ends in the middle of a number')
        byte = buf[i]
        i += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, i
        shift += 7
    raise FormatError('a step holds a number longer than protobuf allows')


def _fields(buf: bytes) -> Iterator[tuple[int, int | bytes]]:
    i, end = 0, len(buf)
    while i < end:
        key, i = _varint(buf, i)
        number, wire = key >> 3, key & 7
        if not number:
            raise FormatError('a step holds a field without a number')
        if wire == 0:
            value, i = _varint(buf, i)
        elif wire in (1, 2, 5):
            size = {1: 8, 5: 4}.get(wire)
            if size is None:
                size, i = _varint(buf, i)
            if i + size > end:
                raise FormatError('a step ends in the middle of a field')
            value, i = buf[i:i + size], i + size
        else:
            raise FormatError(f'a step uses protobuf wire type {wire}')
        yield number, value


class Message:
    """The fields of one protobuf message, by number (the last value wins)."""

    __slots__ = ('fields',)

    def __init__(self, buf: bytes | None = None):
        self.fields: dict[int, list[int | bytes]] = {}
        for number, value in _fields(buf or b''):
            self.fields.setdefault(number, []).append(value)

    def _all(self, number: int, kind: type) -> list:
        values = self.fields.get(number, [])
        if any(not isinstance(v, kind) for v in values):
            raise FormatError(f'field {number} of a step has an unexpected type')
        return values

    def num(self, number: int) -> int:
        values = self._all(number, int)
        return values[-1] if values else 0

    def blob(self, number: int) -> bytes:
        values = self._all(number, bytes)
        return values[-1] if values else b''

    def text(self, number: int) -> str:
        return self.blob(number).decode('utf-8', 'replace')

    def message(self, number: int) -> Message:
        return Message(self.blob(number))

    def messages(self, number: int) -> list[Message]:
        return [Message(v) for v in self._all(number, bytes)]


def created_at(step: Message) -> str:
    seconds = step.message(STEP_METADATA).message(META_CREATED_AT).num(TIME_SECONDS)
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(seconds)) if seconds else ''


def _file_name(uri: str) -> str:
    path = unquote(urlparse(uri).path) if '://' in uri else uri
    return posixpath.basename(path.replace('\\', '/').rstrip('/')) or uri


def _part(part: Message) -> str:
    if PART_TEXT in part.fields:
        return part.text(PART_TEXT)
    if PART_FILE in part.fields:
        return f'`{_file_name(part.message(PART_FILE).text(FILE_URI))}`'
    if PART_FILE_DIFF in part.fields:
        stats = part.message(PART_FILE_DIFF).message(DIFF_STATS)
        return f'(+{stats.num(STATS_ADDED)} −{stats.num(STATS_DELETED)})'
    if PART_AGENT in part.fields:
        return part.message(PART_AGENT).text(AGENT_TITLE)
    return ''


def _arguments(step: Message) -> dict[str, str]:
    """A tool step's arguments: the step's own list, else its tool call's JSON."""
    entries = step.message(STEP_GENERIC).messages(GENERIC_ARGS)
    if entries:
        return {e.text(ENTRY_KEY): e.text(ENTRY_VALUE) for e in entries}
    raw = step.message(STEP_METADATA).message(META_TOOL_CALL).text(CALL_ARGUMENTS)
    try:
        parsed = json.loads(raw) if raw else {}
    except ValueError:
        return {}
    return {str(k): v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
            for k, v in parsed.items()} if isinstance(parsed, dict) else {}


def tool_name(step: Message) -> str:
    return step.message(STEP_METADATA).message(META_TOOL_CALL).text(CALL_NAME)


def _call_line(step: Message) -> str:
    """What a tool did, from its name and arguments, for steps without a display line."""
    name = tool_name(step)
    if not name:
        return ''
    args = _arguments(step)
    target = args.get('AbsolutePath') or args.get('Path') or ''
    detail = (args.get('toolSummary') or args.get('toolAction') or args.get('query')
              or args.get('reason') or (f'`{_file_name(target)}`' if target else ''))
    verb = _TOOL_VERBS.get(name) or name.replace('_', ' ').capitalize()
    return f'{verb} {detail}'.strip()


def tool_line(step: Message) -> str | None:
    """One line saying what a tool step did, as Antigravity shows it; None when hidden."""
    render = step.message(STEP_GENERIC).message(GENERIC_RESULT).message(RESULT_RENDER)
    if render.num(RENDER_HIDDEN):
        return None
    title = ' '.join(p for p in map(_part, render.messages(RENDER_PARTS)) if p)
    prefix = render.text(RENDER_PREFIX)
    line = ' '.join(x for x in (prefix, title or render.text(RENDER_TITLE)) if x)
    line = (line or _call_line(step)).replace('\n', ' ').strip()
    if not line:
        return None
    suffix = _STATUS_SUFFIX.get(step.num(STEP_STATUS))
    if suffix and not line.lower().startswith(suffix[:5]):
        line += f' ({suffix})'
    return line


def _questions(args: dict[str, str]) -> str:
    """The questions the agent asked, with their options, as a numbered list."""
    try:
        asked = json.loads(args.get('questions') or '[]')
    except ValueError:
        asked = []
    lines = []
    for n, item in enumerate(a for a in asked if isinstance(a, dict)):
        lines.append(f'{n + 1}. {str(item.get("question") or "").strip()}')
        lines += [f'   - {option}' for option in item.get('options') or [] if isinstance(option, str)]
    return '**Questions**\n\n' + '\n'.join(lines) if lines else ''


def _answers(step: Message) -> str:
    """The user's answers to the agent's questions (lines `A1: ...` of the result)."""
    result = step.message(STEP_GENERIC).message(GENERIC_RESULT).text(RESULT_TEXT)
    return '\n\n'.join(line.strip() for line in result.splitlines()
                       if _ANSWER.match(line.strip()))


def tool_blocks(step: Message, parent_id: str = '') -> list[tuple[str, str]]:
    """Content a tool step shows besides its line, as (role, text): the plan put
    up for approval, the agent's questions and the user's answers, a message
    sent to another agent (a subagent's report)."""
    name, args = tool_name(step), _arguments(step)
    if name == 'exit_plan_mode' and args.get('plan', '').strip():
        return [('model', f'**Plan**\n\n{args["plan"].strip()}')]
    if name == 'ask_question':
        blocks = [('model', _questions(args)), ('user', _answers(step))]
        return [(role, text) for role, text in blocks if text]
    if name == 'send_message' and args.get('Message', '').strip():
        recipient = args.get('Recipient', '')
        who = 'the parent agent' if parent_id and recipient == parent_id else 'another agent'
        return [('model', f'**Message to {who}**\n\n{args["Message"].strip()}')]
    return []


def agent_message(system: Message) -> tuple[str, str]:
    """(sender, text) of a message from another agent; ('', '') for other system steps."""
    if system.text(SYSTEM_EVENT) != 'agent_message':
        return '', ''
    text = system.text(SYSTEM_TEXT)
    match = _AGENT_MESSAGE.match(text)
    if not match:
        return '', text.strip()
    sender = re.search(r'\bsender=(\S+)', match.group(1))
    return (sender.group(1) if sender else ''), text[match.end():].strip()


def error_text(details: Message) -> str:
    return (details.text(DETAILS_USER_MESSAGE) or details.text(DETAILS_SHORT)).strip()


def _decode_image(data: str) -> bytes:
    try:
        return base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError):
        return b''


def _read_uri(uri: str) -> bytes:
    """Bytes of a local file a message refers to (empty when it is gone)."""
    path = Path(unquote(urlparse(uri).path)) if uri.startswith('file:') else Path(uri)
    try:
        return path.read_bytes() if uri and path.is_file() else b''
    except OSError:
        return b''


def user_images(user: Message) -> list[bytes]:
    images = []
    for image in user.messages(INPUT_IMAGES):
        data = _decode_image(image.text(IMAGE_BASE64)) or _read_uri(image.text(IMAGE_URI))
        if data:
            images.append(data)
    for media in user.messages(INPUT_MEDIA):
        if media.text(MEDIA_MIME).startswith('image/'):
            data = media.blob(MEDIA_INLINE) or _read_uri(media.text(MEDIA_URI))
            if data:
                images.append(data)
    return images


def user_text(user: Message) -> str:
    items = ''.join(item.text(ITEM_TEXT) for item in user.messages(INPUT_ITEMS))
    return (user.text(INPUT_TEXT) or items or user.text(INPUT_QUERY)).strip()


def _result_images(step: Message) -> list[bytes]:
    result = step.message(STEP_GENERIC).message(GENERIC_RESULT)
    return [m.blob(MEDIA_INLINE) for m in result.messages(RESULT_MEDIA)
            if m.text(MEDIA_MIME).startswith('image/') and m.blob(MEDIA_INLINE)]


@dataclass
class _Segment:
    """One turn while it is being read: its paragraphs and a run of tool lines."""
    role: str
    blocks: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    images: list[bytes] = field(default_factory=list)
    thoughts: bool = False
    error: str | None = None

    def flush_tools(self) -> None:
        if self.tools:
            self.blocks.append('\n'.join(f'- *{line}*' for line in self.tools))
            self.tools = []

    def say(self, text: str) -> None:
        if text.strip():
            self.flush_tools()
            self.blocks.append(text.strip())

    def turn(self) -> Turn | None:
        self.flush_tools()
        text = '\n\n'.join(self.blocks)
        if not text and not self.images and not self.thoughts:
            return None
        return Turn(role=self.role, text=text, images=self.images, error=self.error,
                    thought=self.thoughts and not text and not self.images,
                    had_thoughts=self.thoughts)


class Transcript:
    """Turns from steps in order: a user step starts a user turn; everything
    the agent does until the next one is one assistant turn, with a line per
    tool step. Thinking is left out (noted), as are the system's own steps
    (checkpoints, reminders) and the reports subagents send back (their own
    conversations hold them). In a subagent's conversation, the instructions
    from the agent that started it are a user turn that says so."""

    def __init__(self, parent_id: str = '', parent_title: str = ''):
        self.parent_id = parent_id
        self.parent_label = (f'*From the agent of “{parent_title}”:*' if parent_title
                             else '*From the parent agent:*')
        self.segments: list[_Segment] = []
        self.checkpoint_title = ''

    def _current(self, role: str) -> _Segment:
        if not self.segments or self.segments[-1].role != role:
            self.segments.append(_Segment(role))
        return self.segments[-1]

    def add(self, step: Message) -> None:
        kind, status = step.num(STEP_TYPE), step.num(STEP_STATUS)
        if status in (INVALID, CLEARED):
            return
        if kind == USER_INPUT:
            self._user(step.message(STEP_USER_INPUT))
        elif kind == PLANNER_RESPONSE:
            self._planner(step.message(STEP_PLANNER))
        elif kind == ERROR_MESSAGE:
            self._error(error_text(step.message(STEP_ERROR_MESSAGE).message(ERROR_DETAILS)))
        elif kind == CHECKPOINT:
            title = step.message(STEP_CHECKPOINT).text(CHECKPOINT_TITLE).strip()
            self.checkpoint_title = title or self.checkpoint_title
        elif kind == SYSTEM_MESSAGE:
            sender, text = agent_message(step.message(STEP_SYSTEM_MESSAGE))
            if self.parent_id and sender == self.parent_id and text:
                self._new('user').say(f'{self.parent_label}\n\n{text}')
        else:
            self._tool(step)

    def _new(self, role: str) -> _Segment:
        self.segments.append(_Segment(role))
        return self.segments[-1]

    def _user(self, user: Message) -> None:
        segment = self._new('user')
        segment.say(user_text(user))
        segment.images.extend(user_images(user))

    def _planner(self, planner: Message) -> None:
        segment = self._current('model')
        segment.say(planner.text(PLAN_MODIFIED) or planner.text(PLAN_RESPONSE))
        if planner.text(PLAN_THINKING).strip() or planner.text(PLAN_RAW_THINKING).strip():
            segment.thoughts = True

    def _error(self, message: str) -> None:
        if message:
            segment = self._current('model')
            segment.say(f'> Antigravity: {message}')
            segment.error = message

    def _tool(self, step: Message) -> None:
        line = tool_line(step)
        segment = self._current('model')
        if line:
            segment.tools.append(line)
        segment.images.extend(_result_images(step))
        for role, text in tool_blocks(step, self.parent_id):
            if role == 'user':
                self._new('user').say(text)
            else:
                self._current('model').say(text)

    def turns(self) -> list[Turn]:
        return [t for t in (s.turn() for s in self.segments) if t is not None]


def raw_step(index: int, step: Message) -> dict:
    """A step for raw.json: what it was and the text it holds (tool output shortened)."""
    kind = step.num(STEP_TYPE)
    out: dict = {'index': index, 'type': TYPE_NAMES.get(kind, kind),
                 'status': STATUS_NAMES.get(step.num(STEP_STATUS), step.num(STEP_STATUS)),
                 'created_at': created_at(step)}
    if kind == USER_INPUT:
        out['text'] = user_text(step.message(STEP_USER_INPUT))
    elif kind == PLANNER_RESPONSE:
        planner = step.message(STEP_PLANNER)
        out['response'] = planner.text(PLAN_MODIFIED) or planner.text(PLAN_RESPONSE)
        out['thinking'] = planner.text(PLAN_THINKING)
        out['tool_calls'] = [{'name': c.text(CALL_NAME), 'arguments': c.text(CALL_ARGUMENTS)}
                             for c in planner.messages(PLAN_TOOL_CALLS)]
    elif kind == ERROR_MESSAGE:
        out['error'] = error_text(step.message(STEP_ERROR_MESSAGE).message(ERROR_DETAILS))
    elif kind == CHECKPOINT:
        checkpoint = step.message(STEP_CHECKPOINT)
        out['title'] = checkpoint.text(CHECKPOINT_TITLE)
        out['summary'] = checkpoint.text(CHECKPOINT_SUMMARY)
    elif kind == SYSTEM_MESSAGE:
        system = step.message(STEP_SYSTEM_MESSAGE)
        out['event'] = system.text(SYSTEM_EVENT)
        out['text'] = system.text(SYSTEM_TEXT)
    else:
        out.update(_raw_tool(step))
    return out


def _raw_tool(step: Message) -> dict:
    result = step.message(STEP_GENERIC).message(GENERIC_RESULT).text(RESULT_TEXT)
    out = {'tool': tool_name(step), 'line': tool_line(step) or '', 'arguments': _arguments(step),
           'result': result[:_RAW_RESULT_CHARS]}
    if len(result) > _RAW_RESULT_CHARS:
        out['result_chars'] = len(result)
    if STEP_ERROR in step.fields:
        out['error'] = error_text(step.message(STEP_ERROR))
    return out


def subagent_titles(steps: list[Message]) -> dict[str, str]:
    """Titles of the subagents a conversation started, by their conversation id."""
    titles = {}
    for step in steps:
        render = step.message(STEP_GENERIC).message(GENERIC_RESULT).message(RESULT_RENDER)
        for part in render.messages(RENDER_PARTS):
            agent = part.message(PART_AGENT)
            if agent.text(AGENT_CONVERSATION) and agent.text(AGENT_TITLE):
                titles[agent.text(AGENT_CONVERSATION)] = agent.text(AGENT_TITLE).strip()
    return titles
