"""Device integration fixture: the real GUI with a deterministic controller."""

from __future__ import annotations

import tempfile
import time
from pathlib import Path

import flet as ft

from reclaim.core.events import ProgressEvent
from reclaim.core.service import RunSummary
from reclaim.gui import ReclaimWindow


class FakeController:
    busy = False

    def run(self, providers, mode='update', sink=None, *, log=False):
        assert sink is not None
        sink(ProgressEvent('run_started', total=len(providers), message=mode))
        codes = []
        for index, provider in enumerate(providers, 1):
            sink(ProgressEvent('provider_started', provider, current=index,
                               total=len(providers)))
            sink(ProgressEvent('chat_started', provider, 'Example conversation', 1, 1))
            time.sleep(0.03)
            failed = mode == 'rebuild' and index == 1
            sink(ProgressEvent('error' if failed else 'chat_completed', provider,
                               'Example conversation', 1, 1,
                               'Synthetic failure' if failed else ''))
            code = 2 if failed else 0
            codes.append((provider, code))
            sink(ProgressEvent('provider_completed', provider, current=index,
                               total=len(providers), message=f'exit {code}'))
        summary = RunSummary(tuple(codes))
        sink(ProgressEvent('run_completed', current=len(providers),
                           total=len(providers), message=f'exit {summary.exit_code}'))
        return summary


async def main(page: ft.Page) -> None:
    root = Path(tempfile.mkdtemp(prefix='reclaim-flet-test-'))
    window = ReclaimWindow(page, root, FakeController())
    await window.refresh_archive()


ft.run(main)
