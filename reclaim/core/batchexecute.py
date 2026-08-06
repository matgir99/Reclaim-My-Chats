"""Shared batchexecute framing for Google-internal RPC replay.

Used by the `googlegemini` provider (BardChatUi/data/batchexecute) — the
same protocol family the Gemini web app itself speaks. Pure functions
only, no browser imports, so everything here is offline-testable.

Protocol (per research; see docs/status_and_plan/GEMINI_CLAUDE_PLAN.md):

    POST https://gemini.google.com/_/BardChatUi/data/batchexecute
         ?rpcids=<rpcid>&_reqid=<counter>&rt=c&source-path=/app
         &bl=<build label>&f.sid=<session id>
    form: at=<SNlM0e token>&f.req=<build_f_req(rpcid, payload)>

The `f.req` payload is a JSON array STRINGIFIED inside the outer frame —
the classic batchexecute pitfall (build_f_req handles it).

Response: optional ")]}'" anti-XSSI prefix, then length-prefixed frames
(length in UTF-16 code units — non-BMP chars count double). The RPC
result is frame [2], itself a JSON string (decode_response returns it).
"""

from __future__ import annotations

import json


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

    Tolerates whitespace and the optional ")]}'" anti-XSSI prefix. Stops
    at the first non-digit byte that doesn't begin a frame (malformed or
    truncated body).
    """
    text = text.lstrip()
    if text.startswith(")]}'"):
        text = text[4:].lstrip()
    frames: list[str] = []
    i = 0
    while i < len(text):
        j = i
        while j < len(text) and text[j].isdigit():
            j += 1
        if j == i:
            break  # not a frame length -> stop (trailing garbage)
        n = int(text[i:j])
        i = j
        if i + n > _utf16_len(text):
            break  # truncated frame
        frames.append(_slice_utf16(text, i, n))
        i += n
    return frames


def decode_response(text: str) -> list:
    """The RPC result of a batchexecute response.

    Returns the parsed JSON of frame [2] when possible (it is itself a
    JSON string); otherwise the raw frames list so callers can diagnose.
    """
    frames = parse_frames(text)
    if len(frames) >= 3:
        try:
            data = json.loads(frames[2])
            if isinstance(data, list):
                return data
        except Exception:
            pass
    return frames


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
