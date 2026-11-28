"""Direct desktop entry point for Flet's pack and test commands."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _bundled_flet_view_dir() -> Path | None:
    meipass = getattr(sys, '_MEIPASS', None)
    if not meipass:
        return None
    candidate = Path(meipass) / 'flet_desktop' / 'app' / 'flet'
    executable = 'flet.exe' if os.name == 'nt' else 'flet'
    return candidate if (candidate / executable).is_file() else None


def _configure_frozen_env() -> None:
    if not getattr(sys, 'frozen', False):
        return
    # Reuse the Flet client unpacked inside `--onedir` bundles instead of
    # extracting it into ~/.flet/client on first launch.
    bundled = _bundled_flet_view_dir()
    if bundled is not None:
        os.environ.setdefault('FLET_VIEW_PATH', str(bundled))
    # Frozen Linux binaries built on ubuntu-22.04 (glibc 2.35) bundle the
    # matching ubuntu22.04 Flet client archive; pin the lookup so newer
    # distributions reuse the bundled archive instead of downloading another.
    if sys.platform.startswith('linux'):
        os.environ.setdefault('FLET_LINUX_DISTRO', 'ubuntu22.04')


def _main() -> int:
    _configure_frozen_env()
    from reclaim.desktop_smoke import main as smoke_main
    from reclaim.gui import main

    return smoke_main() if sys.argv[1:] == ['--smoke-test'] else main()


if __name__ == '__main__':
    raise SystemExit(_main())
