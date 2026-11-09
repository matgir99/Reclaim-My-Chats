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
from collections.abc import Callable
from urllib.parse import urlsplit

from . import config
from .events import emit

LAUNCH_ARGS = [
    '--disable-blink-features=AutomationControlled',
    '--disable-features=AutomationControlled',
    '--disable-dev-shm-usage',
    '--window-position=-3000,-3000',   # off-screen until login needed
]


class BrowserUnavailableError(RuntimeError):
    """No usable system or Playwright Chromium installation was found."""


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


def launch(p, headless: bool = False, profile_dir: Path | None = None):
    """Launch the shared persistent profile. Returns (ctx, page)."""
    profile = Path(profile_dir) if profile_dir is not None else config.profile_root()
    profile.mkdir(parents=True, exist_ok=True, mode=0o700)
    exe = find_chrome()
    if exe:
        emit(None, 'notice', message=f'Using system browser: {exe}')
    else:
        bundled = Path(p.chromium.executable_path)
        if not bundled.exists():
            raise BrowserUnavailableError(
                'No Chrome or Chromium browser was found. Install Chrome or Chromium '
                'for this computer, then retry. You can also set RECLAIM_CHROME_PATH '
                'to an existing browser executable.')
        emit(None, 'notice',
             message='No system Chrome/Chromium found — using Playwright bundled browser.')
    kwargs = {'user_data_dir': str(profile), 'headless': headless,
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


def on_host(host: str) -> Callable[[str], bool]:
    """Match a page on `host` itself. A URL glob such as
    `**/aistudio.google.com/**` also matches Google's sign-in page, whose
    address carries the destination in `?continue=`."""
    return lambda url: urlsplit(url).hostname == host


def login_timeout() -> int:
    """Seconds to wait for a login in the browser window (RECLAIM_LOGIN_TIMEOUT)."""
    return int(os.environ.get('RECLAIM_LOGIN_TIMEOUT', '900'))


def ask_for_login(page, provider: str, service: str, seconds: int) -> None:
    """Bring the browser window forward and ask the user to log in to
    `service` (a login_needed event: the window and the terminal show it)."""
    page.bring_to_front()
    set_window_bounds(page, state='normal', left=100, top=100)
    minutes = max(1, round(seconds / 60))
    emit(None, 'login_needed', provider,
         message=f'Log in to {service} in the browser window. '
                 f'Waiting up to {minutes} minutes...')


def login_done(page, provider: str) -> None:
    """Report a completed login and hide the browser window again."""
    emit(None, 'logged_in', provider, message='Logged in. Hiding the browser window.')
    set_window_bounds(page, state='minimized')
    time.sleep(2)


def interactive_login(page, check_url_fragment: str, ready_url: str | Callable[[str], bool],
                      provider_name: str, timeout_ms: int = 300000, provider: str = ''):
    """Bring the window forward for a human login, then hide it again.

    No-op when the session is already authenticated (current URL doesn't
    contain check_url_fragment). `provider_name` is the service shown to the
    user ('Google'); `provider` the command the events belong to.
    """
    if check_url_fragment not in page.url:
        return
    ask_for_login(page, provider, provider_name, timeout_ms // 1000)
    page.wait_for_url(ready_url, timeout=timeout_ms)
    login_done(page, provider)
