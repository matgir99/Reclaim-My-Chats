#!/usr/bin/env python3
"""Require exact release versions inside both Python distribution formats."""

from __future__ import annotations

import argparse
import tarfile
import zipfile
from pathlib import Path


def check_version(metadata: str, expected: str, path: Path) -> None:
    values = [line.removeprefix('Version: ').strip() for line in metadata.splitlines()
              if line.startswith('Version: ')]
    if values != [expected]:
        raise ValueError(f'{path}: expected Version: {expected}, found {values}')


def verify(directory: Path, version: str) -> list[Path]:
    wheel = directory / f'reclaimmychats-{version}-py3-none-any.whl'
    source = directory / f'reclaimmychats-{version}.tar.gz'
    for path in (wheel, source):
        if not path.is_file() or path.stat().st_size < 1_000:
            raise ValueError(f'missing or empty Python package: {path}')
    with zipfile.ZipFile(wheel) as archive:
        names = [n for n in archive.namelist() if n.endswith('.dist-info/METADATA')]
        if len(names) != 1:
            raise ValueError(f'{wheel}: expected one METADATA file')
        check_version(archive.read(names[0]).decode(), version, wheel)
    with tarfile.open(source, 'r:gz') as archive:
        names = [n for n in archive.getnames()
                 if n == f'reclaimmychats-{version}/PKG-INFO']
        if len(names) != 1:
            raise ValueError(f'{source}: expected one PKG-INFO file')
        member = archive.extractfile(names[0])
        if member is None:
            raise ValueError(f'{source}: unreadable PKG-INFO')
        check_version(member.read().decode(), version, source)
    return [wheel, source]


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
