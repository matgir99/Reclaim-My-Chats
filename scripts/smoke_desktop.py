#!/usr/bin/env python3
"""Extract and launch a desktop release in an isolated directory."""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import tarfile
import tempfile
import zipfile
from pathlib import Path


def smoke(archive: Path) -> None:
    with tempfile.TemporaryDirectory(prefix='reclaim-desktop-smoke-') as temp:
        root = Path(temp)
        if archive.name.endswith('.tar.gz'):
            with tarfile.open(archive) as bundle:
                bundle.extractall(root, filter='data')
        elif platform.system() == 'Darwin':
            subprocess.run(['ditto', '-x', '-k', str(archive), str(root)], check=True)
        else:
            with zipfile.ZipFile(archive) as bundle:
                bundle.extractall(root)
        system = platform.system()
        binary = root / {
            'Linux': 'ReclaimMyChats/ReclaimMyChats',
            'Windows': 'ReclaimMyChats.exe',
            'Darwin': 'ReclaimMyChats.app/Contents/MacOS/ReclaimMyChats',
        }[system]
        result = root / 'result.json'
        env = dict(os.environ)
        for key in ('PYTHONPATH', 'PYTHONHOME', 'VIRTUAL_ENV'):
            env.pop(key, None)
        env['RECLAIM_HOME'] = str(root / 'data')
        env['RECLAIM_SMOKE_FILE'] = str(result)
        subprocess.run([str(binary), '--smoke-test'], cwd=root, env=env,
                       timeout=90, check=True)
        data = json.loads(result.read_text(encoding='utf-8'))
        if not data.get('ok'):
            raise RuntimeError(f'desktop startup failed: {data.get("error")}')
        expected = {'chatgpt', 'claude', 'googlegemini', 'googleaistudio', 'deepseek', 'kimi'}
        if set(data.get('providers', [])) != expected:
            raise RuntimeError('packaged application is missing providers')
        if Path(data['archive']).resolve() != (root / 'data' / 'chats').resolve():
            raise RuntimeError('packaged application used the wrong archive directory')
        print(f'Packaged GUI and Playwright driver started successfully: {archive.name}')


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archive', type=Path)
    smoke(parser.parse_args().archive.resolve())


if __name__ == '__main__':
    main()
