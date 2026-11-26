#!/usr/bin/env python3
"""Install the built wheel into a fresh environment and use the command line from it.

CI runs this on every pull request and the release workflow runs it before
publishing, so the release's check is exercised long before a release needs it.
The commands run in an empty, temporary RECLAIM_HOME: `init` creates a database
there and `status` must then read it.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import venv
from pathlib import Path


def _wheel(directory: Path) -> Path:
    wheels = sorted(directory.glob('reclaimmychats-*.whl'))
    if len(wheels) != 1:
        raise SystemExit(f'expected one reclaimmychats wheel in {directory}, found {len(wheels)}')
    return wheels[0]


def _python(env_dir: Path) -> Path:
    return env_dir / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')


def _reclaim(python: Path, home: Path, *args: str) -> str:
    """Run `python -I -m reclaim ARGS` outside the checkout; fail loudly with its output."""
    done = subprocess.run(
        [str(python), '-I', '-m', 'reclaim', *args],
        cwd=home.parent, env={**os.environ, 'RECLAIM_HOME': str(home)},
        capture_output=True, encoding='utf-8', errors='replace', check=False,
    )
    if done.returncode != 0:
        raise SystemExit(f'reclaim {" ".join(args)} exited with {done.returncode}:\n'
                         f'{done.stdout}{done.stderr}')
    return done.stdout.strip()


def smoke(directory: Path, version: str | None) -> None:
    wheel = _wheel(directory)
    with tempfile.TemporaryDirectory() as tmp:
        env_dir, home = Path(tmp) / 'env', Path(tmp) / 'home'
        venv.create(env_dir, with_pip=True)
        python = _python(env_dir)
        subprocess.run([str(python), '-m', 'pip', 'install', '--quiet',
                        '--disable-pip-version-check', str(wheel.resolve())], check=True)
        shown = _reclaim(python, home, '--version')
        if version is not None and shown != f'reclaim {version}':
            raise SystemExit(f'expected "reclaim {version}", the wheel says "{shown}"')
        _reclaim(python, home, 'init')
        _reclaim(python, home, 'status')
    print(f'{wheel.name}: installs, reports "{shown}", creates a database and reads its status')


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path, help='folder holding the built wheel')
    parser.add_argument('--version', help='the version the wheel must report')
    args = parser.parse_args(argv)
    smoke(args.directory, args.version)
    return 0


if __name__ == '__main__':
    sys.exit(main())
