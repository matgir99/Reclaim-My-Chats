"""Direct desktop entry point for Flet's pack and test commands."""

import os
import sys


def _configure_frozen_env() -> None:
    # Frozen Linux binaries built on ubuntu-22.04 (glibc 2.35) bundle the
    # matching ubuntu22.04 Flet client archive; pin the lookup so newer
    # distributions reuse the bundled archive instead of downloading another.
    if sys.platform.startswith('linux') and getattr(sys, 'frozen', False):
        os.environ.setdefault('FLET_LINUX_DISTRO', 'ubuntu22.04')


def _main() -> int:
    _configure_frozen_env()
    from reclaim.desktop_smoke import main as smoke_main
    from reclaim.gui import main

    return smoke_main() if sys.argv[1:] == ['--smoke-test'] else main()


if __name__ == '__main__':
    raise SystemExit(_main())
