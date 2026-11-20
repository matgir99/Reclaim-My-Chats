#!/usr/bin/env python3
"""Check that desktop release archives and installers contain expected files."""

from __future__ import annotations

import argparse
import tarfile
import zipfile
from pathlib import Path

MIN_SIZE = 1_000_000


def _verify_linux_tarball(path: Path) -> None:
    if path.stat().st_size < MIN_SIZE:
        raise ValueError(f'{path}: unexpectedly small tarball')
    with tarfile.open(path, 'r:gz') as bundle:
        names = set(bundle.getnames())
    required = {'ReclaimMyChats/ReclaimMyChats', 'ReclaimMyChats/org.reclaimmychats.app.desktop'}
    if not required <= names:
        raise ValueError(f'{path}: missing executable or desktop entry')


def _verify_windows_zip(path: Path) -> None:
    if path.stat().st_size < MIN_SIZE:
        raise ValueError(f'{path}: unexpectedly small Windows archive')
    with zipfile.ZipFile(path) as bundle:
        names = set(bundle.namelist())
    if 'ReclaimMyChats.exe' not in names and 'ReclaimMyChats/ReclaimMyChats.exe' not in names:
        raise ValueError(f'{path}: missing application executable')


def _verify_macos_zip(path: Path) -> None:
    if path.stat().st_size < MIN_SIZE:
        raise ValueError(f'{path}: unexpectedly small macOS archive')
    prefix = 'ReclaimMyChats.app/Contents/MacOS/'
    with zipfile.ZipFile(path) as bundle:
        names = bundle.namelist()
    if not any(n.startswith(prefix) and len(n) > len(prefix) for n in names):
        raise ValueError(f'{path}: missing application executable')


def _verify_optional_binaries(directory: Path, version: str) -> list[Path]:
    checks: list[tuple[str, bytes | None]] = [
        (f'Reclaim-My-Chats-{version}-linux-*.AppImage', b'#!/bin/sh'),
        (f'Reclaim-My-Chats-{version}-linux-*.deb', b'!<arch>\n'),
        (f'Reclaim-My-Chats-{version}-windows-*-setup.exe', b'MZ'),
        (f'Reclaim-My-Chats-{version}-macos-*.dmg', None),
    ]
    found: list[Path] = []
    for pattern, magic in checks:
        for path in sorted(directory.glob(pattern)):
            if path.stat().st_size < MIN_SIZE:
                raise ValueError(f'{path}: unexpectedly small desktop package')
            if magic is not None:
                with path.open('rb') as handle:
                    if not handle.read(len(magic)).startswith(magic):
                        raise ValueError(f'{path}: invalid header signature')
            found.append(path)
    return found


def verify(directory: Path, version: str) -> list[Path]:
    """Validate required and optional desktop distribution files for `version`."""
    found: list[Path] = []
    linux_tar = directory / f'Reclaim-My-Chats-{version}-linux-x86_64.tar.gz'
    if not linux_tar.is_file():
        raise ValueError(f'expected linux archive: {linux_tar.name}')
    _verify_linux_tarball(linux_tar)
    found.append(linux_tar)

    windows_zip = directory / f'Reclaim-My-Chats-{version}-windows-x86_64.zip'
    if not windows_zip.is_file():
        raise ValueError(f'expected windows archive: {windows_zip.name}')
    _verify_windows_zip(windows_zip)
    found.append(windows_zip)

    mac_zips = sorted(directory.glob(f'Reclaim-My-Chats-{version}-macos-*.zip'))
    if not mac_zips:
        raise ValueError(f'expected macos archive: Reclaim-My-Chats-{version}-macos-*.zip')
    for mac_zip in mac_zips:
        _verify_macos_zip(mac_zip)
        found.append(mac_zip)

    found.extend(_verify_optional_binaries(directory, version))
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
