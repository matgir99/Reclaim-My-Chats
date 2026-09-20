#!/usr/bin/env python3
"""Verify GitHub accepted each local asset at the expected size."""

from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> int:
    directory = Path(sys.argv[1])
    response = json.loads(Path(sys.argv[2]).read_text(encoding='utf-8'))
    remote = {item['name']: item['size'] for item in response['assets']}
    local = {path.name: path.stat().st_size for path in directory.iterdir()
             if path.is_file()}
    if len(local) != 6:
        raise ValueError(f'expected six release assets, found {len(local)}')
    if any(remote.get(name) != size for name, size in local.items()):
        raise ValueError(f'GitHub release assets differ from local files: {local} vs {remote}')
    print('verified six uploaded assets')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
