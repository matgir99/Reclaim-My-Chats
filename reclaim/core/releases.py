"""Installed application version and non-blocking GitHub release checks."""

from __future__ import annotations

import json
import os
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version

RELEASES_API = 'https://api.github.com/repos/matgir99/Reclaim-My-Chats/releases/latest'
RELEASES_PAGE = 'https://github.com/matgir99/Reclaim-My-Chats/releases'


@dataclass(frozen=True)
class ReleaseInfo:
    version: str
    url: str


def app_version() -> str:
    """Installed distribution version, or `'dev'` in an editable/uninstalled checkout."""
    try:
        return version('reclaimmychats')
    except PackageNotFoundError:
        return 'dev'


def parse_version(text: str) -> tuple[int, ...] | None:
    """Parse `'3.3.4'` or `'v3.3.4'` into a 3-part integer tuple, else `None`."""
    cleaned = text.strip().lstrip('v')
    parts = cleaned.split('.')
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        return None
    return tuple(int(p) for p in parts)


def is_newer(candidate: str, current: str) -> bool:
    """True when `candidate` is strictly newer than `current`."""
    cand = parse_version(candidate)
    curr = parse_version(current)
    return cand is not None and curr is not None and cand > curr


def _fetch_release_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={'Accept': 'application/vnd.github+json'})
    with urllib.request.urlopen(req, timeout=3) as resp:  # noqa: S310 - HTTPS fixed GitHub API URL
        data = json.loads(resp.read().decode('utf-8'))
    return data if isinstance(data, dict) else {}


def check_for_update(current: str | None = None, *,
                     fetcher: Callable[[str], dict] | None = None) -> ReleaseInfo | None:
    """Return newer release details if available, else None. Never raises."""
    cur = current if current is not None else app_version()
    if os.environ.get('RECLAIM_NO_UPDATE_CHECK') or parse_version(cur) is None:
        return None
    try:
        data = (fetcher or _fetch_release_json)(RELEASES_API)
        tag = str(data.get('tag_name') or '')
        if not tag or not is_newer(tag, cur):
            return None
        url = str(data.get('html_url') or '') or RELEASES_PAGE
        return ReleaseInfo(version=tag.lstrip('v'), url=url)
    except (OSError, ValueError, KeyError, TypeError):
        return None
