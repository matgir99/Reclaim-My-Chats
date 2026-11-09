#!/usr/bin/env python3
"""Report reference projects that changed since the last provider review.

Reads the revisions pinned in docs/UPSTREAM_REVIEW.md (links of the form
github.com/<owner>/<repo>/tree/<sha>), asks GitHub how many commits each
project's default branch has after that revision, and prints a Markdown
report. With --issue it opens or updates one GitHub issue instead (needs
the gh command and GH_TOKEN). The weekly workflow
.github/workflows/upstream-watch.yml runs it.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REVIEW = ROOT / 'docs' / 'UPSTREAM_REVIEW.md'
TITLE = 'Reference projects changed since the last provider review'
PIN = re.compile(r'https://github\.com/([\w.-]+/[\w.-]+)/(?:tree|blob)/([0-9a-f]{40})')


def pinned(text: str) -> dict[str, str]:
    """The last reviewed revision of each project. A later mention (a newer
    review section) replaces an earlier one."""
    found: dict[str, str] = {}
    for repo, sha in PIN.findall(text):
        found[repo] = sha
    return found


def _api(path: str, token: str | None) -> dict:
    headers = {'Accept': 'application/vnd.github+json',
               'User-Agent': 'reclaim-my-chats-upstream-watch'}
    if token:
        headers['Authorization'] = f'Bearer {token}'
    request = urllib.request.Request(f'https://api.github.com/{path}', headers=headers)
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def changes(pins: dict[str, str], token: str | None) -> list[dict]:
    """Projects whose default branch has commits after the reviewed revision."""
    rows = []
    for repo, sha in pins.items():
        branch = _api(f'repos/{repo}', token)['default_branch']
        compare = _api(f'repos/{repo}/compare/{sha}...{branch}', token)
        if compare.get('ahead_by'):
            commits = compare.get('commits') or []
            latest = commits[-1]['commit']['committer']['date'][:10] if commits else ''
            rows.append({'repo': repo, 'sha': sha, 'branch': branch,
                         'ahead': compare['ahead_by'], 'latest': latest})
    return rows


def report(rows: list[dict]) -> str:
    lines = ['These reference projects have commits after the revision last reviewed in '
             '`docs/UPSTREAM_REVIEW.md`. Check the changes for new formats, endpoints or '
             'fixes that apply here, then record the reviewed revisions in that document.',
             '', '| Project | Reviewed | New commits | Latest | Changes |',
             '|:--|:--|--:|:--|:--|']
    for row in rows:
        compare = f"https://github.com/{row['repo']}/compare/{row['sha']}...{row['branch']}"
        lines.append(f"| [{row['repo']}](https://github.com/{row['repo']}) | "
                     f"`{row['sha'][:7]}` | {row['ahead']} | {row['latest']} | "
                     f"[compare]({compare}) |")
    return '\n'.join(lines) + '\n'


def update_issue(body: str) -> str:
    """Open the issue, or replace the body of the open one. Returns its URL."""
    found = subprocess.run(['gh', 'issue', 'list', '--state', 'open', '--search',
                            f'"{TITLE}" in:title', '--json', 'number,title'],
                           check=True, capture_output=True, text=True).stdout
    for issue in json.loads(found or '[]'):
        if issue['title'] == TITLE:
            subprocess.run(['gh', 'issue', 'edit', str(issue['number']), '--body', body],
                           check=True, capture_output=True, text=True)
            return f"updated issue #{issue['number']}"
    created = subprocess.run(['gh', 'issue', 'create', '--title', TITLE, '--body', body],
                             check=True, capture_output=True, text=True).stdout.strip()
    return f'opened {created}'


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or '').splitlines()[0])
    parser.add_argument('--issue', action='store_true',
                        help='open or update the GitHub issue instead of printing')
    args = parser.parse_args(argv)
    pins = pinned(REVIEW.read_text(encoding='utf-8'))
    rows = changes(pins, os.environ.get('GH_TOKEN') or os.environ.get('GITHUB_TOKEN'))
    if not rows:
        print(f'All {len(pins)} reference projects are unchanged since the last review.')
        return 0
    body = report(rows)
    print(update_issue(body) if args.issue else body)
    return 0


if __name__ == '__main__':
    sys.exit(main())
