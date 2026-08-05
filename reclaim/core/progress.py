"""Per-chat progress helpers for scrape run loops.

Compact line (always printed, by the providers):
    [i/N] Title -> Nt, N,NNN chars[, N img][, N doc]
Verbose line (--log only), built by :func:`progress`:
    [i/N] NN% · elapsed M:SS · ETA M:SS
"""

from __future__ import annotations

import time


def _fmt(seconds: float) -> str:
    s = int(seconds)
    return f'{s // 60}:{s % 60:02d}'


def progress(i: int, n: int, t0: float) -> str:
    """Verbose progress line for chat i of n, run started at t0 (epoch s)."""
    elapsed = time.time() - t0
    pct = int(i * 100 / n) if n else 100
    eta = elapsed * (n - i) / i if i and n > i else 0.0
    return f'[{i}/{n}] {pct:3d}% · elapsed {_fmt(elapsed)} · ETA {_fmt(eta)}'
