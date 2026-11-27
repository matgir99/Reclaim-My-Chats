"""ChatGPT Canvas (`canmore.*`) replay into readable assistant Markdown turns.

ChatGPT stores Canvas documents as `canmore.create_textdoc` and
`canmore.update_textdoc` tool calls whose tool replies carry `metadata.canvas`.
Because updates store search/replace edits rather than full snapshots, each
document is reconstructed in branch order and emitted inline where the tool
reply appeared without mutating raw payloads.

Adapted from pionxzh/chatgpt-exporter `src/utils/canvas.ts` (MIT,
revision 9364915).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

_CREATE = 'canmore.create_textdoc'
_UPDATE = 'canmore.update_textdoc'
_COMMENT = 'canmore.comment_textdoc'
_CANVAS_TOOLS = frozenset({_CREATE, _UPDATE, _COMMENT})
_FULL_REWRITE_PATTERNS = frozenset({'.*', '(?s).*', '^.*$', '(?s)^.*$', r'[\s\S]*', r'^[\s\S]*$'})
_EDIT_UNAVAILABLE = 'This edit could not be applied to the earlier text, so only the new text is shown.'
_FENCE_RUNS = re.compile(r'`+')


@dataclass
class _CanvasDoc:
    title: str
    kind: str
    version: int | None = None
    content: str | None = None


def _escape_label(text: str) -> str:
    escaped = text.replace('\\', '\\\\').replace('[', '\\[').replace(']', '\\]')
    return f'**{escaped}**'


def _format_body(doc_type: str, content: str) -> str:
    if not doc_type.startswith('code/'):
        return content
    lang = doc_type[len('code/'):].replace('\n', '')
    lang = 'jsx' if lang == 'react' else lang
    fence = '`' * max(3, max((len(m[0]) + 1 for m in _FENCE_RUNS.finditer(content)), default=3))
    return f'{fence}{lang}\n{content}\n{fence}'


def _format_canvas(doc: _CanvasDoc, label: str) -> str:
    return f'{_escape_label(label)}\n\n{_format_body(doc.kind, doc.content or "")}'


def _call_text(call: dict) -> str:
    content = call.get('content') or {}
    ctype = content.get('content_type')
    if ctype == 'text':
        return ''.join(p for p in (content.get('parts') or []) if isinstance(p, str))
    if ctype == 'code':
        text = content.get('text')
        return text if isinstance(text, str) else ''
    return ''


def _escape_controls_in_strings(text: str) -> str:
    out: list[str] = []
    in_string = False
    i = 0
    escapes = {'\n': '\\n', '\r': '\\r', '\t': '\\t'}
    while i < len(text):
        ch = text[i]
        if in_string and ch == '\\':
            nxt = text[i + 1] if i + 1 < len(text) else ''
            out.append(ch + nxt)
            i += 2
            continue
        if ch == '"':
            in_string = not in_string
        out.append(escapes[ch] if in_string and ch in escapes else ch)
        i += 1
    return ''.join(out)


def _parse_canvas_json(text: str) -> dict | None:
    if not text:
        return None
    for candidate in (text, _escape_controls_in_strings(text)):
        try:
            parsed = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _expand_literal(template: str) -> str | None:
    empty = re.match(r'(?s).*', '')
    assert empty is not None
    try:
        return empty.expand(template)
    except (re.error, IndexError):
        return None


def _apply_update(content: str, pattern: str, replacement: str, *, multiple: bool) -> str | None:
    try:
        regex = re.compile(pattern, re.DOTALL)
        matches = list(regex.finditer(content)) if multiple else (
            [m] if (m := regex.search(content)) else []
        )
        if not matches:
            return None
        parts: list[str] = []
        end = 0
        for match in matches:
            parts.append(content[end:match.start()])
            parts.append(match.expand(replacement))
            end = match.end()
        parts.append(content[end:])
        return ''.join(parts)
    except (re.error, IndexError):
        return None


def _valid_update(item) -> tuple[str, str, bool] | None:
    if not isinstance(item, dict):
        return None
    pattern, replacement = item.get('pattern'), item.get('replacement')
    if not isinstance(pattern, str) or not isinstance(replacement, str):
        return None
    return pattern, replacement, item.get('multiple') is True


def _replay_update(doc: _CanvasDoc, body: dict | None, known: bool) -> str | None:
    raw_updates = body.get('updates') if isinstance(body, dict) else None
    updates = [u for item in (raw_updates if isinstance(raw_updates, list) else ())
               if (u := _valid_update(item)) is not None]
    if not updates:
        return None
    content = doc.content if known else None
    for pattern, replacement, multiple in updates:
        rewrite = pattern in _FULL_REWRITE_PATTERNS
        if content is None:
            content = _expand_literal(replacement) if rewrite else None
        else:
            # A rewrite selects the whole text on its first match. With
            # `multiple`, `finditer` would also yield the empty match at the
            # end and append the new text a second time.
            content = _apply_update(content, pattern, replacement,
                                    multiple=multiple and not rewrite)
    doc.content = content
    label = f'Canvas: {doc.title} (edited)'
    if content is not None:
        return _format_canvas(doc, label)
    snippets = [_format_body(doc.kind, _expand_literal(rep) or rep) for _, rep, _ in updates]
    return '\n\n'.join([_escape_label(label), _EDIT_UNAVAILABLE, *snippets])


def _str_or(value, default: str) -> str:
    return value if isinstance(value, str) and value else default


def _int_or_none(value) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _replay_create(meta: dict, body: dict | None,
                   textdoc_id: str, documents: dict[str, _CanvasDoc]) -> str:
    extra = body or {}
    raw_content = extra.get('content')
    doc = _CanvasDoc(
        title=_str_or(meta.get('title'), _str_or(extra.get('name'), 'Canvas')),
        kind=_str_or(meta.get('textdoc_type'), _str_or(extra.get('type'), 'document')),
        version=_int_or_none(meta.get('version')),
        content=raw_content if isinstance(raw_content, str) else '',
    )
    documents[textdoc_id] = doc
    return _format_canvas(doc, f'Canvas: {doc.title}')


def _replay_step(name: str, call: dict, meta: dict, textdoc_id: str,
                 documents: dict[str, _CanvasDoc]) -> str | None:
    body = _parse_canvas_json(_call_text(call))
    if name == _CREATE:
        return _replay_create(meta, body, textdoc_id, documents)
    doc = documents.get(textdoc_id) or _CanvasDoc(
        title=_str_or(meta.get('title'), 'Canvas'),
        kind=_str_or(meta.get('textdoc_type'), 'document'),
    )
    from_ver = meta.get('from_version')
    known = doc.content is not None and (from_ver is None or doc.version is None or from_ver == doc.version)
    version = _int_or_none(meta.get('version'))
    doc.version = version if version is not None else doc.version
    documents[textdoc_id] = doc
    if name == _COMMENT:
        if not known:
            doc.content = None
        return None
    return _replay_update(doc, body, known)


def _matches_canvas_call(name: str, call: dict) -> bool:
    recipient = call.get('recipient')
    if recipient is not None:
        return recipient == name
    # Official exports can omit `recipient`; require assistant role and JSON tool payload.
    return ((call.get('author') or {}).get('role') == 'assistant'
            and _parse_canvas_json(_call_text(call)) is not None)


def _hide_call_node(out: list[dict], call: dict) -> None:
    """Mark an unrecipiented Canvas call hidden in `out` without mutating `mapping`."""
    if call.get('recipient') is not None:
        return
    for idx in range(len(out) - 1, -1, -1):
        prev = out[idx]
        if prev.get('message') is call:
            meta = dict(call.get('metadata') or {})
            meta['is_visually_hidden_from_conversation'] = True
            out[idx] = {**prev, 'message': {**call, 'metadata': meta}}
            return


def _canvas_reply_context(node: dict, mapping: dict) -> tuple[str, dict, dict] | None:
    reply = node.get('message') or {}
    author = reply.get('author') or {}
    name = author.get('name') if author.get('role') == 'tool' else None
    meta = (reply.get('metadata') or {}).get('canvas') if isinstance(reply.get('metadata'), dict) else None
    parent = mapping.get(node.get('parent')) if node.get('parent') else None
    call = parent.get('message') if isinstance(parent, dict) else None
    if (name in _CANVAS_TOOLS and isinstance(meta, dict) and not meta.get('is_failure')
            and isinstance(meta.get('textdoc_id'), str) and meta['textdoc_id']
            and isinstance(call, dict) and _matches_canvas_call(name, call)):
        return name, call, meta
    return None


def replay_canvases(nodes: list[dict], mapping: dict) -> list[dict]:
    """Convert successful Canvas tool replies along `nodes` into assistant turns.

    Leaves `mapping` and original node dicts untouched so raw payloads stay clean.
    """
    documents: dict[str, _CanvasDoc] = {}
    out: list[dict] = []
    for node in nodes:
        ctx = _canvas_reply_context(node, mapping)
        if ctx is not None:
            name, call, meta = ctx
            _hide_call_node(out, call)
            block = _replay_step(name, call, meta, meta['textdoc_id'], documents)
            if block:
                reply = node['message']
                out.append({
                    **node,
                    'message': {
                        **reply,
                        'author': {'role': 'assistant', 'metadata': {}},
                        'recipient': 'all',
                        'channel': call.get('channel'),
                        'content': {'content_type': 'text', 'parts': [block]},
                        'metadata': {'is_canvas_block': True},
                    },
                })
                continue
        out.append(node)
    return out
