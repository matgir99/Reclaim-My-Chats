#!/usr/bin/env python3.14
"""Scrape DeepSeek Chat conversations — backwards-compatible wrapper.

The implementation lives in `reclaim.providers.deepseek` (IndexedDB
architecture). This shim keeps the old entry point working:

    python3.14 scrape_deepseek.py [TITLE] [--rebuild] [--list] ...

See docs/PLAN.md and docs/ARCHITECTURE.md.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from reclaim.providers.deepseek import main

if __name__ == '__main__':
    raise SystemExit(main())
