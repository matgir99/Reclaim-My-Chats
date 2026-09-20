#!/usr/bin/env python3
"""Build and archive a Flet desktop application for the current OS."""

from __future__ import annotations

import argparse
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_NAME = 'ReclaimMyChats'
ARCHIVE_NAME = 'Reclaim-My-Chats'


def build(version: str, output: Path) -> Path:
    system = platform.system().lower()
    if system not in ('linux', 'windows', 'darwin'):
        raise RuntimeError(f'unsupported desktop platform: {system}')
    label = {'darwin': 'macos', 'windows': 'windows', 'linux': 'linux'}[system]
    machine = platform.machine().lower()
    arch = {'amd64': 'x86_64', 'x64': 'x86_64', 'aarch64': 'arm64'}.get(machine, machine)
    stage = ROOT / 'build' / 'desktop-pack'
    stage.mkdir(parents=True, exist_ok=True)
    flet = shutil.which('flet')
    if flet is None:
        raise RuntimeError('flet CLI is missing; install the gui-test extra')
    launcher = [flet] if os.name == 'nt' else [sys.executable, flet]
    env = dict(os.environ)
    env['PYTHONPATH'] = os.pathsep.join(filter(None, [str(ROOT), env.get('PYTHONPATH')]))
    subprocess.run([*launcher,
        'pack', str(ROOT / 'desktop' / 'main.py'),
        '--name', APP_NAME, '--product-name', 'Reclaim My Chats',
        '--product-version', version, '--bundle-id', 'org.reclaimmychats.app',
        '--distpath', str(stage), '--yes',
    ], cwd=ROOT, env=env, check=True)

    output.mkdir(parents=True, exist_ok=True)
    stem = f'{ARCHIVE_NAME}-{version}-{label}-{arch}'
    if system == 'linux':
        binary = stage / APP_NAME
        desktop = stage / 'org.reclaimmychats.app.desktop'
        for item in (binary, desktop):
            if not item.is_file():
                raise RuntimeError(f'flet pack did not produce {item}')
        # Flet writes the CI build path into Exec. Ship a relocatable template.
        lines = desktop.read_text(encoding='utf-8').splitlines()
        desktop.write_text('\n'.join('Exec=ReclaimMyChats' if line.startswith('Exec=')
                                     else line for line in lines) + '\n',
                           encoding='utf-8')
        archive = output / f'{stem}.tar.gz'
        with tarfile.open(archive, 'w:gz') as bundle:
            bundle.add(binary, arcname=f'{APP_NAME}/{binary.name}')
            bundle.add(desktop, arcname=f'{APP_NAME}/{desktop.name}')
    elif system == 'windows':
        binary = stage / f'{APP_NAME}.exe'
        if not binary.is_file():
            raise RuntimeError(f'flet pack did not produce {binary}')
        archive = output / f'{stem}.zip'
        with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED) as bundle:
            bundle.write(binary, arcname=binary.name)
    else:
        app = stage / f'{APP_NAME}.app'
        if not app.is_dir():
            raise RuntimeError(f'flet pack did not produce {app}')
        archive = output / f'{stem}.zip'
        subprocess.run(['ditto', '-c', '-k', '--sequesterRsrc', '--keepParent',
                        str(app), str(archive)], check=True)
    if archive.stat().st_size < 1_000_000:
        raise RuntimeError(f'desktop archive is unexpectedly small: {archive}')
    return archive


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version', required=True)
    parser.add_argument('--output-dir', type=Path, default=ROOT / 'dist' / 'release')
    args = parser.parse_args()
    if not re.fullmatch(r'\d+\.\d+\.\d+', args.version):
        parser.error('version must be X.Y.Z')
    print(build(args.version, args.output_dir.resolve()))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
