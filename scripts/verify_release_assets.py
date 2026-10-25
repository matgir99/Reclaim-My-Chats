#!/usr/bin/env python3
"""Verify GitHub accepted each local asset at the expected size."""

from __future__ import annotations

import json
import hashlib
import sys
from pathlib import Path


def verify(directory: Path, response: dict) -> None:
    remote = {item['name']: item for item in response['assets']}
    local = {path.name: path for path in directory.iterdir() if path.is_file()}
    if len(local) != 6:
        raise ValueError(f'expected six release assets, found {len(local)}')
    if set(remote) != set(local):
        raise ValueError('GitHub release asset names differ from local files')
    for name, path in local.items():
        expected = 'sha256:' + hashlib.sha256(path.read_bytes()).hexdigest()
        if (remote[name]['size'] != path.stat().st_size
                or remote[name].get('digest') != expected):
            raise ValueError(f'GitHub release asset size or SHA-256 differs: {name}')
    print('verified six uploaded assets and their SHA-256 digests')


def main() -> int:
    directory = Path(sys.argv[1])
    response = json.loads(Path(sys.argv[2]).read_text(encoding='utf-8'))
    verify(directory, response)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
