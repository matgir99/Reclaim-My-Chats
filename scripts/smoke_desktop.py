#!/usr/bin/env python3
"""Extract or install desktop release packages in isolation and launch them."""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import tarfile
import tempfile
import zipfile
from pathlib import Path

EXPECTED_PROVIDERS = {'chatgpt', 'claude', 'googlegemini', 'googleaistudio', 'deepseek', 'kimi'}


def _windows_zip_binary(root: Path, archive: Path) -> Path:
    with zipfile.ZipFile(archive) as bundle:
        bundle.extractall(root)
    onedir = root / 'ReclaimMyChats' / 'ReclaimMyChats.exe'
    return onedir if onedir.is_file() else (root / 'ReclaimMyChats.exe')


def _macos_dmg_binary(root: Path, archive: Path) -> Path:
    mount = root / 'dmg-mount'
    mount.mkdir()
    subprocess.run(['hdiutil', 'attach', '-nobrowse', '-readonly',
                    '-mountpoint', str(mount), str(archive)], check=True)
    try:
        if not (mount / 'Applications').is_symlink():
            raise RuntimeError(f'{archive.name}: missing Applications symlink inside DMG')
        app = root / 'ReclaimMyChats.app'
        subprocess.run(['ditto', str(mount / 'ReclaimMyChats.app'), str(app)], check=True)
        return app / 'Contents' / 'MacOS' / 'ReclaimMyChats'
    finally:
        subprocess.run(['hdiutil', 'detach', str(mount), '-force'], check=False)


def _prepare_binary(archive: Path, root: Path) -> Path:
    name = archive.name
    if name.endswith('.tar.gz'):
        with tarfile.open(archive) as bundle:
            bundle.extractall(root, filter='data')
        return root / 'ReclaimMyChats' / 'ReclaimMyChats'
    if name.endswith('.AppImage'):
        copy = root / name
        shutil.copy2(archive, copy)
        copy.chmod(copy.stat().st_mode | 0o755)
        return copy
    if name.endswith('.deb'):
        subprocess.run(['dpkg-deb', '-x', str(archive), str(root)], check=True)
        return root / 'opt' / 'ReclaimMyChats' / 'ReclaimMyChats'
    if name.endswith('-setup.exe'):
        installed = root / 'installed'
        subprocess.run([str(archive), '/VERYSILENT', '/SUPPRESSMSGBOXES',
                        '/NORESTART', '/SP-', f'/DIR={installed}'], check=True)
        return installed / 'ReclaimMyChats.exe'
    if name.endswith('.dmg'):
        return _macos_dmg_binary(root, archive)
    if name.endswith('.zip') and platform.system() == 'Darwin':
        subprocess.run(['ditto', '-x', '-k', str(archive), str(root)], check=True)
        return root / 'ReclaimMyChats.app' / 'Contents' / 'MacOS' / 'ReclaimMyChats'
    if name.endswith('.zip'):
        return _windows_zip_binary(root, archive)
    raise RuntimeError(f'unsupported desktop artifact: {archive}')


def smoke(archive: Path) -> None:
    """Launch `archive` in an isolated RECLAIM_HOME and verify providers + startup."""
    with tempfile.TemporaryDirectory(prefix='reclaim-desktop-smoke-') as temp:
        root = Path(temp)
        fake_home = root / 'home'
        fake_home.mkdir()
        binary = _prepare_binary(archive, root)
        result = root / 'result.json'
        env = dict(os.environ)
        for key in ('PYTHONPATH', 'PYTHONHOME', 'VIRTUAL_ENV', 'FLET_VIEW_PATH'):
            env.pop(key, None)
        env['HOME'] = str(fake_home)
        env['USERPROFILE'] = str(fake_home)
        env['XDG_CACHE_HOME'] = str(root / 'cache')
        env['RECLAIM_HOME'] = str(root / 'data')
        env['RECLAIM_SMOKE_FILE'] = str(result)
        subprocess.run([str(binary), '--smoke-test'], cwd=root, env=env,
                       timeout=90, check=True)
        uses_preunpacked_client = (
            archive.name.endswith(('.tar.gz', '.AppImage', '.deb', '-setup.exe'))
            or (archive.name.endswith('.zip') and platform.system() != 'Darwin')
        )
        if uses_preunpacked_client and (fake_home / '.flet').exists():
            raise RuntimeError(f'{archive.name}: extracted Flet client into ~/.flet at runtime')
        data = json.loads(result.read_text(encoding='utf-8'))
        if not data.get('ok'):
            raise RuntimeError(f'desktop startup failed: {data.get("error")}')
        if set(data.get('providers', [])) != EXPECTED_PROVIDERS:
            raise RuntimeError('packaged application is missing providers')
        if Path(data['archive']).resolve() != (root / 'data' / 'chats').resolve():
            raise RuntimeError('packaged application used the wrong archive directory')
        print(f'Packaged GUI and Playwright driver started successfully: {archive.name}')


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archives', nargs='+', type=Path)
    for archive in parser.parse_args().archives:
        smoke(archive.resolve())


if __name__ == '__main__':
    main()
