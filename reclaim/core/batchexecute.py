"""Shared batchexecute framing for Google-internal RPC replay.

Used by the `googlegemini` provider (BardChatUi/data/batchexecute) — the
same protocol family the Gemini web app itself speaks. Pure functions
only, no browser imports, so everything here is offline-testable.

Protocol (per MIT references; see docs/UPSTREAM_REVIEW.md):

    POST https://gemini.google.com/_/BardChatUi/data/batchexecute
         ?rpcids=<rpcid>&_reqid=<counter>&rt=c&source-path=/app
         &bl=<build label>&f.sid=<session id>
    form: at=<SNlM0e token>&f.req=<build_f_req(rpcid, payload)>

The `f.req` payload is a JSON array STRINGIFIED inside the outer frame —
the classic batchexecute pitfall (build_f_req handles it).

Response: optional ")]}'" anti-XSSI prefix, then length-prefixed frames
(length in UTF-16 code units — non-BMP chars count double). The RPC
result is the JSON string at index 2 of the matching `wrb.fr` record.
"""

from __future__ import annotations

import json
from collections import deque


def build_f_req(rpcid: str, payload) -> str:
    """The `f.req` form value for one RPC call.

    Shape: [[["<rpcid>", "<payload as JSON string>", null, "generic"]]]
    """
    return json.dumps([[[rpcid, json.dumps(payload), None, 'generic']]])


def _utf16_len(s: str) -> int:
    return len(s.encode('utf-16-le')) // 2


def _slice_utf16(s: str, start_units: int, n_units: int) -> str:
    """Take n UTF-16 units from s starting at start_units, code-point safe.

    Frame lengths are in UTF-16 units, so a surrogate pair (non-BMP char,
    e.g. an emoji) must never be split; chars that don't fit wholly inside
    the slice are skipped.
    """
    out: list[str] = []
    used = 0
    for ch in s:
        u = 2 if ord(ch) > 0xFFFF else 1
        if start_units <= used and used + u <= start_units + n_units:
            out.append(ch)
        used += u
    return ''.join(out)


def parse_frames(text: str) -> list[str]:
    """Split a batchexecute response body into its frames.

    Wire format: optional ")]}'" anti-XSSI prefix, then per frame
    `{length}\n{content}` — the length counts the content's UTF-16 units
    (the separating newline is not counted). Tolerates a missing newline
    and stops cleanly at trailing garbage or a truncated final frame.
    """
    text = text.lstrip()
    text = text.removeprefix(")]}'")
    frames: list[str] = []
    i = 0   # code-point index into text
    u = 0   # UTF-16-unit offset of text[i] from the string start
    n_chars = len(text)
    while i < n_chars:
        if not text[i].isdigit():
            if text[i].isspace():
                i += 1
                u += 1
                continue
            break  # non-whitespace garbage -> stop
        j = i
        while j < n_chars and text[j].isdigit():
            j += 1
        n = int(text[i:j])
        u += j - i  # digits are BMP
        i = j
        if i < n_chars and text[i] == '\n':
            i += 1
            u += 1
        frame = _slice_utf16(text, u, n)
        if _utf16_len(frame) < n:
            break  # truncated frame -> drop it and stop
        frames.append(frame)
        i += len(frame)
        u += n
    return frames


def _json_list(text) -> list | None:
    """`text` parsed as a JSON array, else None."""
    try:
        value = json.loads(text)
    except (ValueError, TypeError):
        return None
    return value if isinstance(value, list) else None


def _rpc_result(arrays: list[list], rpcid: str | None) -> tuple[bool, list | None]:
    """Search the arrays, breadth first, for the record of RPC `rpcid`
    (['wrb.fr', rpcid, '<result JSON>', ...]). Returns (whether any RPC
    record was seen, the matching record's result or None)."""
    seen_rpc = False
    queue = deque(arrays)
    while queue:
        array = queue.popleft()
        if not (array and array[0] == 'wrb.fr'):
            queue.extend(item for item in array if isinstance(item, list))
            continue
        seen_rpc = True
        if len(array) < 3 or (rpcid is not None and array[1] != rpcid):
            continue
        try:
            result = json.loads(array[2])
        except (ValueError, TypeError) as exc:
            raise RuntimeError('invalid JSON in batchexecute RPC result') from exc
        if not isinstance(result, list):
            raise RuntimeError('missing batchexecute RPC result')
        return True, result
    return seen_rpc, None


def decode_response(text: str, rpcid: str | None = None) -> list:
    """Decode the matching RPC record, independent of telemetry/frame order.

    Google also serves unframed JSON lines. Legacy captures containing a
    bare payload in the third frame remain readable. Unknown/error pages
    raise instead of masquerading as an empty conversation list.
    """
    frames = parse_frames(text)
    cleaned = text.lstrip()
    if cleaned.startswith(")]}'"):
        cleaned = cleaned[4:].lstrip()
    fragments = [*frames, cleaned, *cleaned.splitlines()]
    arrays = [array for array in map(_json_list, fragments) if array is not None]
    seen_rpc, result = _rpc_result(arrays, rpcid)
    if result is not None:
        return result
    if seen_rpc:
        raise RuntimeError(f'batchexecute response has no result for RPC {rpcid}')
    # Saved captures: a bare payload in the third frame, or the whole body.
    # Never return strings or protocol telemetry as if they were chat data.
    for payload in (frames[2] if len(frames) >= 3 else None, cleaned):
        result = _json_list(payload)
        if result is not None:
            return result
    raise RuntimeError('unrecognized batchexecute response; login or protocol may have changed')


# Known error codes from the Gemini web client (gemini-webapi research).
# 0/absent = success; others surface as the decoded payload's first int.
ERROR_CODES = {
    1013: 'transient error (retry)',
    1037: 'quota exceeded',
    1060: 'IP rate-limited (back off)',
}


def error_message(decoded) -> str | None:
    """Batchexecute error code if the decoded payload starts with one."""
    if (isinstance(decoded, list) and decoded
            and isinstance(decoded[0], int) and decoded[0] != 0):
        return f'{decoded[0]} ({ERROR_CODES.get(decoded[0], "unknown")})'
    return None
