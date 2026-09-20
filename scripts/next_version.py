#!/usr/bin/env python3
"""Choose the idempotent release version for the checked-out commit.

The first GUI release starts the 3.3 series. Later releases increment the
patch component of the highest reachable release tag. Tags on another branch
never silently determine the next version.
"""

from __future__ import annotations

import argparse
import re
import subprocess

TAG = re.compile(r'^v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$')


def git(*args: str) -> str:
    return subprocess.check_output(['git', *args], text=True).strip()


def next_version(expected_ref: str | None = None) -> str:
    head = git('rev-parse', 'HEAD')
    if expected_ref is not None and head != git('rev-parse', expected_ref):
        raise ValueError(f'HEAD is not {expected_ref}; refusing to release a stale commit')

    tags = []
    for tag in git('tag', '--list').splitlines():
        match = TAG.fullmatch(tag)
        if match:
            commit = git('rev-list', '-n', '1', tag)
            version = tuple(map(int, match.groups()))
            tags.append((version, tag, commit))

    at_head = [entry for entry in tags if entry[2] == head]
    if at_head:
        if len(at_head) != 1:
            raise ValueError('multiple release tags point at HEAD')
        chosen = at_head[0][1]
        if chosen != max(tags)[1]:
            raise ValueError(f'{chosen} at HEAD is older than another release tag')
        return chosen.removeprefix('v')

    reachable = [entry for entry in tags
                 if subprocess.run(['git', 'merge-base', '--is-ancestor', entry[2], head],
                                   check=False).returncode == 0]
    if not reachable:
        raise ValueError('no reachable vX.Y.Z release tag')
    latest = max(reachable)[0]
    proposed = (3, 3, 0) if latest == (3, 2, 0) else (latest[0], latest[1], latest[2] + 1)
    candidate = '.'.join(map(str, proposed))
    collision = next((entry for entry in tags if entry[1] == f'v{candidate}'), None)
    if collision:
        raise ValueError(f'v{candidate} already points at another commit: {collision[2]}')
    return candidate


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--require-ref', help='Require HEAD to match this Git ref')
    args = parser.parse_args()
    try:
        print(next_version(args.require_ref))
    except (ValueError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f'release version error: {exc}\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
