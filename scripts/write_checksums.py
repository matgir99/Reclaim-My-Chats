#!/usr/bin/env python3
"""Write SHA-256 checksums for every release package."""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path


def main() -> int:
    directory = Path(sys.argv[1])
    files = sorted(p for p in directory.iterdir()
                   if p.is_file() and p.name != 'SHA256SUMS')
    if len(files) != 5:
        raise ValueError(f'expected five package artifacts, found {len(files)}')
    lines = []
    for path in files:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        lines.append(f'{digest}  {path.name}')
    (directory / 'SHA256SUMS').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    print('verified five packages and wrote SHA256SUMS')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
