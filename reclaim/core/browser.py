"""Browser helpers shared by scrape-mode providers.

Wraps Playwright persistent-context launch (shared profile, anti-detection
flags, off-screen window) and CDP window management so the window only ever
appears when a human login is needed.
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import shutil
import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from . import config
from .events import emit

_active_page: Any = None
_active_owner: int | None = None
_active_lock = threading.Lock()
_bring_forward_requested = threading.Event()
_no_wait = threading.local()

_PLAYWRIGHT_CHROMIUM_PATHS = (
    ('chrome-linux64', 'chrome'),
    ('chrome-linux-arm64', 'chrome'),
    ('chrome-linux', 'chrome'),
    ('chrome-mac-arm64', 'Google Chrome for Testing.app', 'Contents', 'MacOS',
     'Google Chrome for Testing'),
    ('chrome-mac-x64', 'Google Chrome for Testing.app', 'Contents', 'MacOS',
     'Google Chrome for Testing'),
    ('chrome-mac', 'Chromium.app', 'Contents', 'MacOS', 'Chromium'),
    ('chrome-win64', 'chrome.exe'),
    ('chrome-win', 'chrome.exe'),
)


class LoginRequiredError(RuntimeError):
    """Raised when login is needed while wait-free checks (`no_login_wait`) are active."""


class LoginCancelledError(RuntimeError):
    """Raised when the user stops the run while waiting for a manual login."""


def _should_stop() -> bool:
    from . import engine
    return engine.should_stop()


@contextmanager
def no_login_wait() -> Iterator[None]:
    """Fail immediately instead of waiting for manual login (e.g. startup checks)."""
    prev = getattr(_no_wait, 'active', False)
    _no_wait.active = True
    try:
        yield
    finally:
        _no_wait.active = prev


def login_wait_disabled() -> bool:
    """True inside a `no_login_wait()` block."""
    return bool(getattr(_no_wait, 'active', False))

LAUNCH_ARGS = [
    '--disable-blink-features=AutomationControlled',
    '--disable-features=AutomationControlled',
    '--disable-dev-shm-usage',
    '--window-position=-3000,-3000',   # off-screen until login needed
]


class BrowserUnavailableError(RuntimeError):
    """No usable system or Playwright Chromium installation was found."""


def find_chrome(root: Path | None = None) -> str | None:
    """Locate a system Chrome/Chromium binary, or None to use Playwright's bundled one.

    Order: RECLAIM_CHROME_PATH env var -> saved settings -> known OS paths -> PATH lookup.
    """
    env = os.environ.get('RECLAIM_CHROME_PATH', '').strip()
    if env:
        return env
    with contextlib.suppress(config.SettingsError):
        configured = config.chrome_path(root)
        if configured:
            return configured
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


def _playwright_package_dir() -> Path | None:
    meipass = getattr(sys, '_MEIPASS', None)
    if meipass:
        frozen_pkg = Path(meipass) / 'playwright'
        if frozen_pkg.is_dir():
            return frozen_pkg
    with contextlib.suppress(ValueError):
        spec = importlib.util.find_spec('playwright')
        if spec is not None and spec.origin and spec.origin != 'frozen':
            return Path(spec.origin).resolve().parent
    return None


def _playwright_chromium_revision() -> str | None:
    pkg_dir = _playwright_package_dir()
    if pkg_dir is None:
        return None
    manifest = pkg_dir / 'driver' / 'package' / 'browsers.json'
    with contextlib.suppress(OSError, ValueError, TypeError):
        entries = json.loads(manifest.read_text(encoding='utf-8')).get('browsers') or []
        for entry in entries:
            if entry.get('name') == 'chromium' and entry.get('revision'):
                return str(entry['revision'])
    return None


def _playwright_cache_dir() -> Path | None:
    env = os.environ.get('PLAYWRIGHT_BROWSERS_PATH', '').strip()
    if env == '0':
        pkg_dir = _playwright_package_dir()
        return (pkg_dir / 'driver' / 'package' / '.local-browsers') if pkg_dir else None
    if env:
        return Path(env).expanduser().resolve()
    if sys.platform == 'darwin':
        return Path.home() / 'Library' / 'Caches' / 'ms-playwright'
    if os.name == 'nt' or sys.platform in ('win32', 'nt', 'cygwin'):
        base = os.environ.get('LOCALAPPDATA') or str(Path.home() / 'AppData' / 'Local')
        return Path(base) / 'ms-playwright'
    base = os.environ.get('XDG_CACHE_HOME') or str(Path.home() / '.cache')
    return Path(base) / 'ms-playwright'


def find_playwright_chromium() -> str | None:
    """Locate Playwright's cached Chromium binary for the current revision, if installed."""
    revision = _playwright_chromium_revision()
    cache_dir = _playwright_cache_dir()
    if not revision or cache_dir is None:
        return None
    install_dir = cache_dir / f'chromium-{revision}'
    for parts in _PLAYWRIGHT_CHROMIUM_PATHS:
        candidate = install_dir.joinpath(*parts)
        if candidate.is_file():
            return str(candidate)
    return None


def launch(p, headless: bool = False, profile_dir: Path | None = None, *,
           root: Path | None = None):
    """Launch the shared persistent profile. Returns (ctx, page)."""
    profile = Path(profile_dir) if profile_dir is not None else config.profile_root(root)
    profile.mkdir(parents=True, exist_ok=True, mode=0o700)
    exe = find_chrome(root)
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
    _register_active_page(page)
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


def login_timeout(root: Path | None = None) -> int:
    """Seconds to wait for a login in the browser window (`RECLAIM_LOGIN_TIMEOUT` or settings)."""
    with contextlib.suppress(config.SettingsError):
        return config.login_timeout(root)
    return int(os.environ.get('RECLAIM_LOGIN_TIMEOUT', '900'))


def _register_active_page(page) -> None:
    global _active_page, _active_owner
    with _active_lock:
        _active_page = page
        _active_owner = threading.get_ident()
        _bring_forward_requested.clear()


def clear_active_page() -> None:
    """Clear the active Playwright page reference when a browser session ends."""
    global _active_page, _active_owner
    with _active_lock:
        _active_page = None
        _active_owner = None
        _bring_forward_requested.clear()


def _focus_page(page) -> bool:
    with contextlib.suppress(Exception):
        page.bring_to_front()
        set_window_bounds(page, state='normal', left=100, top=100)
        return True
    return False


def _flush_bring_forward(page) -> None:
    if page is not None and _bring_forward_requested.is_set():
        _bring_forward_requested.clear()
        _focus_page(page)


def bring_browser_forward(page=None) -> bool:
    """Bring the active Playwright browser window to the front.

    When called from outside the Playwright worker thread (such as the GUI
    event loop), queue the focus request for the active login wait loop instead
    of touching the sync Playwright object cross-thread.
    """
    if page is not None:
        _bring_forward_requested.clear()
        return _focus_page(page)
    with _active_lock:
        target, owner = _active_page, _active_owner
        if target is None:
            return False
        if owner is not None and threading.get_ident() != owner:
            _bring_forward_requested.set()
            return True
    _bring_forward_requested.clear()
    return _focus_page(target)


def ask_for_login(page, provider: str, service: str, seconds: int) -> None:
    """Bring the browser window forward and ask the user to log in to
    `service` (a login_needed event: the window and the terminal show it)."""
    _register_active_page(page)
    bring_browser_forward(page)
    minutes = max(1, round(seconds / 60))
    emit(None, 'login_needed', provider,
         message=f'Log in to {service} in the browser window. '
                 f'Waiting up to {minutes} minutes...')


def login_done(page, provider: str) -> None:
    """Report a completed login and hide the browser window again."""
    emit(None, 'logged_in', provider, message='Logged in. Hiding the browser window.')
    set_window_bounds(page, state='minimized')
    time.sleep(2)


def _try_probe(probe: Callable[[], Any]) -> Any:
    try:
        return probe()
    except Exception:
        return None


def _sleep_stopping(interval: float, page=None) -> bool:
    """Sleep up to `interval` seconds while polling cancellation and focus requests; True when stopped."""
    _flush_bring_forward(page)
    if interval <= 0.2:
        if interval > 0:
            time.sleep(interval)
        _flush_bring_forward(page)
        return _should_stop()
    end = time.time() + interval
    while time.time() < end:
        if _should_stop():
            return True
        _flush_bring_forward(page)
        time.sleep(min(0.2, max(0.0, end - time.time())))
    _flush_bring_forward(page)
    return _should_stop()


def wait_for_login(page, provider: str, service: str,
                   probe: Callable[[], Any], *, interval: float = 3.0) -> Any:
    """Return `probe()` once authenticated, waiting in the browser window if needed."""
    result = _try_probe(probe)
    if result:
        return result
    if login_wait_disabled():
        emit(None, 'login_needed', provider, message=f'{service} requires a login.')
        raise LoginRequiredError(f'{service} login required')
    timeout = login_timeout()
    ask_for_login(page, provider, service, timeout)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _should_stop():
            set_window_bounds(page, state='minimized')
            raise LoginCancelledError(f'{service} login stopped by user')
        _flush_bring_forward(page)
        result = _try_probe(probe)
        if result:
            login_done(page, provider)
            return result
        if _sleep_stopping(interval, page):
            set_window_bounds(page, state='minimized')
            raise LoginCancelledError(f'{service} login stopped by user')
    raise RuntimeError(f'{service} login timeout (Login timeout)')


def _wait_url_slice(page, ready_url: str | Callable[[str], bool], chunk_ms: int) -> bool:
    try:
        page.wait_for_url(ready_url, timeout=chunk_ms)
        return True
    except Exception as exc:
        if 'timeout' in str(exc).lower() or type(exc).__name__ == 'TimeoutError':
            return False
        raise


def interactive_login(page, check_url_fragment: str, ready_url: str | Callable[[str], bool],
                      provider_name: str, timeout_ms: int | None = None, provider: str = ''):
    """Bring the window forward for a human login, then hide it again.

    No-op when the session is already authenticated (current URL doesn't
    contain check_url_fragment). `provider_name` is the service shown to the
    user ('Google'); `provider` the command the events belong to.
    """
    if check_url_fragment not in page.url:
        return
    if login_wait_disabled():
        emit(None, 'login_needed', provider, message=f'{provider_name} requires a login.')
        raise LoginRequiredError(f'{provider_name} login required')
    wait_ms = timeout_ms if timeout_ms is not None else login_timeout() * 1000
    ask_for_login(page, provider, provider_name, wait_ms // 1000)
    deadline = time.time() + (wait_ms / 1000)
    while True:
        if _should_stop():
            set_window_bounds(page, state='minimized')
            raise LoginCancelledError(f'{provider_name} login stopped by user')
        _flush_bring_forward(page)
        remaining_ms = max(1, int((deadline - time.time()) * 1000))
        if _wait_url_slice(page, ready_url, min(500, remaining_ms)):
            break
        if time.time() >= deadline:
            page.wait_for_url(ready_url, timeout=1)
    login_done(page, provider)
