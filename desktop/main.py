"""Direct desktop entry point for Flet's pack and test commands."""

import sys

from reclaim.gui import main
from reclaim.desktop_smoke import main as smoke_main


if __name__ == '__main__':
    raise SystemExit(smoke_main() if sys.argv[1:] == ['--smoke-test'] else main())
