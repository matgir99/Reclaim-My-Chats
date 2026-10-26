#!/usr/bin/env python3
"""Resume a draft release, or leave an already published release untouched."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

try:
    from .verify_release_assets import verify
except ImportError:  # Direct script execution from GitHub Actions.
    from verify_release_assets import verify

def existing_release(repository: str, tag: str) -> dict | None:
    result = subprocess.run(['gh', 'api', f'repos/{repository}/releases/tags/{tag}'],
                            text=True, capture_output=True, check=False)
    if result.returncode == 0:
        return json.loads(result.stdout)
    if 'HTTP 404' not in result.stderr:
        raise RuntimeError(f'cannot check existing release: {result.stderr.strip()}')

    # The tag endpoint can return 404 for a draft. The authenticated release
    # list includes drafts, including ones left behind by an interrupted run.
    query = f'.[] | select(.tag_name == {json.dumps(tag)}) | .id'
    listed = subprocess.run(['gh', 'api', '--paginate',
                             f'repos/{repository}/releases?per_page=100', '--jq', query],
                            text=True, capture_output=True, check=False)
    if listed.returncode != 0:
        raise RuntimeError(f'cannot list release drafts: {listed.stderr.strip()}')
    identifiers = listed.stdout.split()
    if not identifiers:
        return None
    if len(identifiers) != 1 or not identifiers[0].isdigit():
        raise RuntimeError(f'expected one release for {tag}; refusing an ambiguous lookup')
    found = subprocess.run(['gh', 'api', f'repos/{repository}/releases/{identifiers[0]}'],
                           text=True, capture_output=True, check=False)
    if found.returncode != 0:
        raise RuntimeError(f'cannot read release draft: {found.stderr.strip()}')
    return json.loads(found.stdout)


def publish(repository: str, tag: str, directory: Path) -> None:
    release = existing_release(repository, tag)
    if release is not None and not release['draft']:
        print(f'{tag} is already published; its downloads will not be changed.')
        return
    if release is None:
        subprocess.run(['gh', 'release', 'create', tag, '--repo', repository,
                        '--verify-tag', '--generate-notes', '--draft', '--title', tag],
                       check=True)
    files = sorted(str(p) for p in directory.iterdir() if p.is_file())
    subprocess.run(['gh', 'release', 'upload', tag, '--repo', repository,
                    *files, '--clobber'], check=True)
    # This check is against the server's SHA-256 digests, not just file sizes.
    uploaded = existing_release(repository, tag)
    if uploaded is None or not uploaded['draft']:
        raise RuntimeError('release is no longer a draft; refusing to publish')
    verify(directory, uploaded)
    subprocess.run(['gh', 'release', 'edit', tag, '--repo', repository, '--draft=false'],
                   check=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', required=True)
    parser.add_argument('--tag', required=True)
    parser.add_argument('directory', type=Path)
    args = parser.parse_args()
    publish(args.repo, args.tag, args.directory)


if __name__ == '__main__':
    main()
