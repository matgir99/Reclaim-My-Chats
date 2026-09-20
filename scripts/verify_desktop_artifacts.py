#!/usr/bin/env python3
"""Check that all three desktop archives contain the expected application."""

from __future__ import annotations

import argparse
import tarfile
import zipfile
from pathlib import Path


def verify(directory: Path, version: str) -> list[Path]:
    patterns = {
        'linux': f'Reclaim-My-Chats-{version}-linux-x86_64.tar.gz',
        'windows': f'Reclaim-My-Chats-{version}-windows-x86_64.zip',
        'macos': f'Reclaim-My-Chats-{version}-macos-*.zip',
    }
    found = []
    for system, pattern in patterns.items():
        paths = list(directory.glob(pattern))
        if len(paths) != 1 or paths[0].stat().st_size < 1_000_000:
            raise ValueError(f'expected one nonempty {system} archive: {pattern}')
        path = paths[0]
        if system == 'linux':
            with tarfile.open(path, 'r:gz') as bundle:
                names = set(bundle.getnames())
                if not {'ReclaimMyChats/ReclaimMyChats',
                        'ReclaimMyChats/org.reclaimmychats.app.desktop'} <= names:
                    raise ValueError(f'{path}: missing executable or desktop entry')
        else:
            with zipfile.ZipFile(path) as bundle:
                names = bundle.namelist()
                expected = ('ReclaimMyChats.exe' if system == 'windows'
                            else 'ReclaimMyChats.app/Contents/MacOS/')
                if not any(n == expected if system == 'windows'
                           else n.startswith(expected) and len(n) > len(expected)
                           for n in names):
                    raise ValueError(f'{path}: missing application executable')
        found.append(path)
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version', required=True)
    parser.add_argument('directory', type=Path)
    args = parser.parse_args()
    for path in verify(args.directory, args.version):
        print(f'verified {path}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
