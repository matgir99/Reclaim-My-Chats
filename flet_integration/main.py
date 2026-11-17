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

    def capture(self, provider, url, sink=None, destination=None):
        assert sink is not None
        path = Path(destination or (Path(tempfile.gettempdir()) / f'{provider}-capture.json'))
        sink(ProgressEvent('run_started', total=1, message='capture'))
        sink(ProgressEvent('provider_started', provider, current=1, total=1))
        sink(ProgressEvent('captured', provider, message='1 response recorded', detail=str(path)))
        sink(ProgressEvent('provider_completed', provider, current=1, total=1, message='exit 0'))
        summary = RunSummary(((provider, 0),))
        sink(ProgressEvent('run_completed', current=1, total=1, message='exit 0'))
        return summary


async def main(page: ft.Page) -> None:
    root = Path(tempfile.mkdtemp(prefix='reclaim-flet-test-'))
    window = ReclaimWindow(page, root, FakeController())
    await window.refresh_archive()


ft.run(main)
