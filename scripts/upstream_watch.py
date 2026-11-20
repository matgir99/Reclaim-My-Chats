#!/usr/bin/env python3
"""Report upstream changes to review: reference projects and dependency releases.

Reads the revisions pinned in docs/UPSTREAM_REVIEW.md (links of the form
github.com/<owner>/<repo>/tree/<sha>) and asks GitHub how many commits each
project's default branch has after that revision. It also compares the
versions of the runtime dependencies in WATCHED that uv.lock installs with
their newest releases on PyPI. It prints a Markdown report; with --issue it
opens or updates one GitHub issue instead (needs the gh command and
GH_TOKEN). The weekly workflow .github/workflows/upstream-watch.yml runs it.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tomllib
import urllib.request
from datetime import date
from pathlib import Path
from typing import NamedTuple

ROOT = Path(__file__).resolve().parents[1]
REVIEW = ROOT / 'docs' / 'UPSTREAM_REVIEW.md'
LOCK = ROOT / 'uv.lock'
TITLE = 'Upstream changes to review'
# Before dependency releases were added; an open issue with it is taken over.
OLD_TITLE = 'Reference projects changed since the last provider review'
AGENT = 'reclaim-my-chats-upstream-watch'
PIN = re.compile(r'https://github\.com/([\w.-]+/[\w.-]+)/(?:tree|blob)/([0-9a-f]{40})')
FINAL = re.compile(r'(\d+)\.(\d+)(?:\.(\d+))?')


class Watched(NamedTuple):
    grace_days: int  # report a newer minor or major release this long after it appeared
    notes: str       # where its release notes are


# Runtime dependencies whose newer minor or major releases are reported.
# Flet is upgraded by hand (docs/GUI.md), so a new release is reported at
# once. Dependabot offers Playwright with uv.lock every month, so it is
# reported only when that update is overdue.
WATCHED = {
    'flet': Watched(0, 'https://github.com/flet-dev/flet/releases'),
    'playwright': Watched(45, 'https://github.com/microsoft/playwright-python/releases'),
}


def pinned(text: str) -> dict[str, str]:
    """The last reviewed revision of each project. A later mention (a newer
    review section) replaces an earlier one."""
    return dict(PIN.findall(text))


def _api(path: str, token: str | None) -> dict:
    headers = {'Accept': 'application/vnd.github+json',
               'User-Agent': AGENT}
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


def locked_versions(text: str) -> dict[str, str]:
    """Package versions that uv.lock installs (this project's own entry has none)."""
    return {package['name']: package['version'] for package in tomllib.loads(text)['package']
            if 'version' in package}


def _final(version: str) -> tuple[int, int, int] | None:
    """(major, minor, patch) of a final release; None for pre-releases and other forms."""
    match = FINAL.fullmatch(version)
    return (int(match[1]), int(match[2]), int(match[3] or 0)) if match else None


def _uploaded(files: list[dict]) -> str | None:
    """Upload day of a release, or None when it has no files that are not yanked."""
    days = [file['upload_time_iso_8601'][:10] for file in files if not file.get('yanked')]
    return min(days) if days else None


def newer_release(name: str, locked: str, releases: dict[str, list[dict]],
                  today: date) -> dict | None:
    """The newest release of a higher minor or major version than `locked`, once the
    first such release is older than the package's grace period."""
    current = _final(locked)
    if current is None:
        raise ValueError(f'uv.lock installs {name} {locked}, which is not a final release')
    newer = {}
    for version, files in releases.items():
        parsed, uploaded = _final(version), _uploaded(files)
        if parsed and uploaded and parsed[:2] > current[:2]:
            newer[parsed] = (version, uploaded)
    if not newer:
        return None
    first = min(uploaded for _, uploaded in newer.values())
    if (today - date.fromisoformat(first)).days < WATCHED[name].grace_days:
        return None
    version, uploaded = newer[max(newer)]
    return {'package': name, 'locked': locked, 'newest': version, 'released': uploaded,
            'notes': WATCHED[name].notes}


def _pypi_releases(name: str) -> dict[str, list[dict]]:
    request = urllib.request.Request(f'https://pypi.org/pypi/{name}/json',
                                     headers={'Accept': 'application/json', 'User-Agent': AGENT})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)['releases']


def releases_behind(locked: dict[str, str], today: date) -> list[dict]:
    """Watched dependencies with a newer minor or major release than uv.lock installs."""
    missing = sorted(set(WATCHED) - set(locked))
    if missing:
        raise ValueError(f'uv.lock does not install {", ".join(missing)}; update WATCHED')
    rows = []
    for name in WATCHED:
        row = newer_release(name, locked[name], _pypi_releases(name), today)
        if row:
            rows.append(row)
    return rows


def _projects_section(rows: list[dict]) -> list[str]:
    lines = ['## Reference projects', '',
             ('These reference projects have commits after the revision last reviewed in '
              '`docs/UPSTREAM_REVIEW.md`. Check the changes for new formats, endpoints or '
              'fixes that apply here, then record the reviewed revisions in that document.'),
             '', '| Project | Reviewed | New commits | Latest | Changes |',
             '|:--|:--|--:|:--|:--|']
    for row in rows:
        compare = f"https://github.com/{row['repo']}/compare/{row['sha']}...{row['branch']}"
        lines.append(f"| [{row['repo']}](https://github.com/{row['repo']}) | "
                     f"`{row['sha'][:7]}` | {row['ahead']} | {row['latest']} | "
                     f"[compare]({compare}) |")
    return lines


def _releases_section(rows: list[dict]) -> list[str]:
    lines = ['## Dependency releases', '',
             ('These runtime dependencies have a newer minor or major release than '
              '`uv.lock` installs. Upgrade Flet by hand, in `pyproject.toml`, `uv.lock` '
              'and `flet_integration/pyproject.toml` together, then check the window '
              '(`docs/GUI.md`). Playwright normally arrives with Dependabot\'s monthly '
              'update, so it is listed only once that is overdue; check why it did not.'),
             '', '| Package | Installed | Newest | Released | Notes |',
             '|:--|:--|:--|:--|:--|']
    lines.extend(f"| {row['package']} | {row['locked']} | {row['newest']} | "
                 f"{row['released']} | [release notes]({row['notes']}) |" for row in rows)
    return lines


def report(projects: list[dict], releases: list[dict]) -> str:
    sections = []
    if projects:
        sections.append(_projects_section(projects))
    if releases:
        sections.append(_releases_section(releases))
    return '\n\n'.join('\n'.join(lines) for lines in sections) + '\n'


def _open_issues(title: str) -> list[dict]:
    found = subprocess.run(['gh', 'issue', 'list', '--state', 'open', '--search',
                            f'"{title}" in:title', '--json', 'number,title'],
                           check=True, capture_output=True, text=True).stdout
    return [issue for issue in json.loads(found or '[]') if issue['title'] == title]


def update_issue(body: str) -> str:
    """Open the issue, or replace the title and body of the open one. Returns its URL."""
    existing = _open_issues(TITLE) or _open_issues(OLD_TITLE)
    if existing:
        number = str(existing[0]['number'])
        subprocess.run(['gh', 'issue', 'edit', number, '--title', TITLE, '--body', body],
                       check=True, capture_output=True, text=True)
        return f'updated issue #{number}'
    created = subprocess.run(['gh', 'issue', 'create', '--title', TITLE, '--body', body],
                             check=True, capture_output=True, text=True).stdout.strip()
    return f'opened {created}'


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or '').splitlines()[0])
    parser.add_argument('--issue', action='store_true',
                        help='open or update the GitHub issue instead of printing')
    args = parser.parse_args(argv)
    pins = pinned(REVIEW.read_text(encoding='utf-8'))
    projects = changes(pins, os.environ.get('GH_TOKEN') or os.environ.get('GITHUB_TOKEN'))
    releases = releases_behind(locked_versions(LOCK.read_text(encoding='utf-8')), date.today())
    if not projects and not releases:
        print(f'All {len(pins)} reference projects are unchanged since the last review, '
              f'and {" and ".join(WATCHED)} have no newer release to review.')
        return 0
    body = report(projects, releases)
    print(update_issue(body) if args.issue else body)
    return 0


if __name__ == '__main__':
    sys.exit(main())
