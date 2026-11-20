"""Requests to a provider: one retry policy for every provider.

Providers make their requests from inside the logged-in browser page. Each
request is a function returning the page-side response:
`{'status': int, 'text': str, 'retryAfter': str | None}`. `send` decides
what happens next, the same way for every service:

- 200 is returned.
- 401 and 403 stop at once: retrying cannot repair an expired login.
- 429 (rate limited) waits as long as the service asks in Retry-After (at
  most five minutes), otherwise 15, 30, 60 and 120 seconds. These waits do
  not use up the ordinary retries.
- 500, 502, 503, 504 and failed requests (a page reload, a network drop)
  are retried with short pauses.
- Any other status stops, unless the caller asked to handle it itself.

A request that fails for good raises `RequestFailed`, which carries the HTTP
status of the last response. Decide what a failure means from that status, as
`is_not_found` does, never from the message: the message can contain "404" for
other reasons, such as the digits of a chat id.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager

RATE_LIMIT_WAITS = (15, 30, 60, 120)
MAX_RATE_LIMIT_WAIT = 300
TRANSIENT = (500, 502, 503, 504)
_recorder = threading.local()


class RequestFailed(RuntimeError):
    """A request that ended without a usable response.

    `status` is the HTTP status of the last response, or None when no response
    arrived (a page reload, a network drop)."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class AuthError(RequestFailed):
    """The service refused the login (HTTP 401 or 403)."""


def is_not_found(exc: BaseException) -> bool:
    """True when the service answered HTTP 404, which is how a chat that no
    longer exists is recognised. Any other failure (a rate limit, a server
    error, a refused login, a dropped connection) says nothing about the chat."""
    return isinstance(exc, RequestFailed) and exc.status == 404


@contextmanager
def recording() -> Iterator[list[dict]]:
    """Record every response `send` receives in this thread during the block."""
    previous = getattr(_recorder, 'responses', None)
    responses: list[dict] = []
    _recorder.responses = responses
    try:
        yield responses
    finally:
        _recorder.responses = previous


def _record(what: str, response: dict) -> None:
    recorded = getattr(_recorder, 'responses', None)
    if recorded is not None:
        recorded.append({'request': what, 'status': response.get('status'),
                         'text': str(response.get('text') or '')})


def rate_limit_wait(retry_after, index: int) -> float:
    """Seconds to wait after the `index`-th rate limit in a row."""
    try:
        seconds = float(retry_after)
    except (TypeError, ValueError):
        return RATE_LIMIT_WAITS[index]
    return min(max(seconds, 1.0), MAX_RATE_LIMIT_WAIT)


def _backoff(attempt: int, retries: int) -> None:
    """A short pause before the next attempt (2 s, then 4 s...), none after the last."""
    if attempt < retries:
        time.sleep(2 * attempt)


def send(call: Callable[[], dict], *, what: str, retries: int = 3,
         give_back: tuple[int, ...] = (), return_errors: bool = False,
         auth_retries: int = 0, record: bool = True) -> dict:
    """Run `call` until it gives a final response, and return that response.

    `give_back` lists statuses the caller handles itself (for example a 404
    meaning "deleted"). `return_errors` returns every final response instead
    of raising, for callers that fall back to another method; a service that
    keeps rate limiting still raises, since a fallback would be limited too. `auth_retries`
    allows a few retries of 401/403 after a short pause, for services whose
    page finishes setting up its credentials after loading.

    Three separate budgets: `retries` for failed requests and server errors,
    RATE_LIMIT_WAITS for 429s, and `auth_retries` for 401/403."""
    last = 'no response'
    last_status: int | None = None
    attempt = limited = auth = 0
    while attempt < retries:
        try:
            response = call()
        except Exception as exc:  # a request that failed inside the page
            last, last_status = str(exc), None
            attempt += 1
            _backoff(attempt, retries)
            continue
        if record:
            _record(what, response)
        status = response.get('status')
        last_status = status if isinstance(status, int) else None
        if status == 200 or status in give_back:
            return response
        if status in (401, 403) and auth < auth_retries:
            auth += 1
            time.sleep(2)
            continue
        if status == 429 and limited < len(RATE_LIMIT_WAITS):
            time.sleep(rate_limit_wait(response.get('retryAfter'), limited))
            limited += 1
            continue
        if status == 429:  # every rate-limit wait is used up
            last = 'HTTP 429: rate limited by the service; retry later'
            break
        final = status in (401, 403) or status not in TRANSIENT or attempt + 1 >= retries
        if final and return_errors:
            return response
        if status in (401, 403):
            raise AuthError(f'{what}: HTTP {status}, the login was refused. '
                            'Log in again and retry.', status)
        last = f'HTTP {status}: {str(response.get("text", ""))[:120]}'
        if final:
            break
        attempt += 1
        _backoff(attempt, retries)
    raise RequestFailed(f'{what} failed: {last}', last_status)
