"""Browser helpers shared by scrape-mode providers.

Wraps Playwright persistent-context launch (shared profile, anti-detection
flags, off-screen window) and CDP window management so the window only ever
appears when a human login is needed.
"""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
USER_DATA_DIR = str(REPO_ROOT / '.playwright-profile')

LAUNCH_ARGS = [
    '--disable-blink-features=AutomationControlled',
    '--disable-features=AutomationControlled',
    '--disable-dev-shm-usage',
    '--window-position=-3000,-3000',   # off-screen until login needed
]


def find_chrome() -> str | None:
    """Locate a system Chrome/Chromium binary, or None to use Playwright's bundled one.

    Order: RECLAIM_CHROME_PATH env var -> known OS paths -> PATH lookup.
    """
    env = os.environ.get('RECLAIM_CHROME_PATH')
    if env:
        return env
    candidates = [
        # Linux
        '/usr/bin/google-chrome-stable',
        '/usr/bin/google-chrome',
        '/usr/bin/chromium',
        '/usr/bin/chromium-browser',
        # macOS
        '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
        '/Applications/Chromium.app/Contents/MacOS/Chromium',
        # Windows
        r'C:\Program Files\Google\Chrome\Application\chrome.exe',
        r'C:\Program Files (x86)\Google\Chrome\Application\chrome.exe',
        r'C:\Program Files\Chromium\Application\chrome.exe',
    ]
    for c in candidates:
        if Path(c).exists():
            return c
    for name in ('google-chrome-stable', 'google-chrome', 'chromium',
                 'chromium-browser', 'chrome', 'msedge'):
        p = shutil.which(name)
        if p:
            return p
    return None


def launch(p, headless: bool = False):
    """Launch the shared persistent profile. Returns (ctx, page)."""
    Path(USER_DATA_DIR).mkdir(parents=True, exist_ok=True)
    exe = find_chrome()
    if exe:
        print(f'Using system browser: {exe}')
    else:
        print('No system Chrome/Chromium found — using Playwright bundled browser.')
    kwargs = {'user_data_dir': USER_DATA_DIR, 'headless': headless,
              'args': LAUNCH_ARGS,
              'viewport': {'width': 1400, 'height': 900}}
    if exe:
        kwargs['executable_path'] = exe
    ctx = p.chromium.launch_persistent_context(**kwargs)
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    return ctx, page


def set_window_bounds(page, state='normal', left=None, top=None):
    """Set browser window state via CDP ('normal' to show, 'minimized' to hide)."""
    try:
        cdp = page.context.new_cdp_session(page)
        target = cdp.send('Browser.getWindowForTarget')
        bounds = {'windowState': state}
        if left is not None:
            bounds['left'] = left
        if top is not None:
            bounds['top'] = top
        cdp.send('Browser.setWindowBounds',
                 {'windowId': target['windowId'], 'bounds': bounds})
        cdp.detach()
    except Exception:
        pass


def interactive_login(page, check_url_fragment: str, ready_url_glob: str,
                      provider_name: str, timeout_ms: int = 300000):
    """Bring the window forward for a human login, then hide it again.

    No-op when the session is already authenticated (current URL doesn't
    contain check_url_fragment).
    """
    if check_url_fragment not in page.url:
        return
    page.bring_to_front()
    set_window_bounds(page, state='normal', left=100, top=100)
    print('\n' + '=' * 50)
    print(f'  LOG IN to {provider_name} in the browser window.')
    print('  Waiting (up to 5 minutes)...')
    print('=' * 50 + '\n')
    page.wait_for_url(ready_url_glob, timeout=timeout_ms)
    print('Logged in! Hiding window...\n')
    set_window_bounds(page, state='minimized')
    time.sleep(2)
