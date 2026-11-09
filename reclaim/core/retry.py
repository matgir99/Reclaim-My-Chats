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
"""

from __future__ import annotations

import time
from typing import Callable

RATE_LIMIT_WAITS = (15, 30, 60, 120)
MAX_RATE_LIMIT_WAIT = 300
TRANSIENT = (500, 502, 503, 504)


class AuthError(RuntimeError):
    """The service refused the login (HTTP 401 or 403)."""


def rate_limit_wait(retry_after, index: int) -> float:
    """Seconds to wait after the `index`-th rate limit in a row."""
    try:
        seconds = float(retry_after)
    except (TypeError, ValueError):
        return RATE_LIMIT_WAITS[index]
    return min(max(seconds, 1.0), MAX_RATE_LIMIT_WAIT)


def send(call: Callable[[], dict], *, what: str, retries: int = 3,
         give_back: tuple[int, ...] = (), return_errors: bool = False,
         auth_retries: int = 0) -> dict:
    """Run `call` until it gives a final response, and return that response.

    `give_back` lists statuses the caller handles itself (for example a 404
    meaning "deleted"). `return_errors` returns every final response instead
    of raising, for callers that fall back to another method. `auth_retries`
    allows a few retries of 401/403 after a short pause, for services whose
    page finishes setting up its credentials after loading."""
    last = 'no response'
    attempt = limited = auth = 0
    while attempt < retries:
        try:
            response = call()
        except Exception as exc:  # a request that failed inside the page
            last = str(exc)
            attempt += 1
            if attempt < retries:
                time.sleep(2 * attempt)
            continue
        status = response.get('status')
        if status == 200 or status in give_back:
            return response
        if status in (401, 403):
            if auth < auth_retries:
                auth += 1
                time.sleep(2)
                continue
            if return_errors:
                return response
            raise AuthError(f'{what}: HTTP {status}, the login was refused. '
                            'Log in again and retry.')
        if status == 429:
            last = 'HTTP 429: rate limited by the service; retry later'
            if limited == len(RATE_LIMIT_WAITS):
                break
            time.sleep(rate_limit_wait(response.get('retryAfter'), limited))
            limited += 1
            continue
        last = f'HTTP {status}: {str(response.get("text", ""))[:120]}'
        if status not in TRANSIENT:
            if return_errors:
                return response
            break
        attempt += 1
        if attempt < retries:
            time.sleep(2 * attempt)
        elif return_errors:
            return response
    raise RuntimeError(f'{what} failed: {last}')
