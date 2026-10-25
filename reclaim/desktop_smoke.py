"""Exercise the actual packaged desktop runtime without provider accounts."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import flet as ft

from .core import config
from .core.service import ALL_PROVIDERS, provider_module
from .gui import ReclaimWindow


def _check_provider_runtime() -> None:
    from playwright.sync_api import sync_playwright

    for name in ALL_PROVIDERS:
        provider_module(name)
    # Starting the driver catches missing bundled Playwright/Node files.
    # No browser or provider connection is needed.
    with sync_playwright() as playwright:
        assert playwright.chromium.executable_path


def main() -> int:
    result = Path(os.environ['RECLAIM_SMOKE_FILE'])

    async def check(page) -> None:
        try:
            window = ReclaimWindow(page)
            await window.refresh_archive()
            await asyncio.to_thread(_check_provider_runtime)
            await page.window.center()
            expected = Path(os.environ['RECLAIM_HOME']).resolve()
            if config.default_root() != expected:
                raise RuntimeError('packaged application did not use its isolated data folder')
            result.write_text(json.dumps({
                'ok': True, 'providers': list(window.checks),
                'archive': str(config.archive_root()),
            }), encoding='utf-8')
        except Exception as exc:
            result.write_text(json.dumps({'ok': False, 'error': str(exc)}), encoding='utf-8')
        finally:
            await page.window.destroy()

    ft.run(check)
    return 0
