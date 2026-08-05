"""ReclaimMyChats unified CLI.

Usage:
  reclaim <provider> [TITLE] [options]     update: new + changed chats only
  reclaim <provider> --rebuild [options]   everything, freshly (overwrite)
  reclaim <provider> "latex"               chats whose title contains "latex"
  reclaim <provider> --url URL             one exact chat
  reclaim <provider> --list [TITLE]        print chat titles, no download
  reclaim <provider> --log [options]       verbose progress + timings
  reclaim <provider> --dry-run [options]   preview what would be fetched
  reclaim status [-o DIR]                  offline archive overview
  reclaim all [options]                    update all four providers, in order
  reclaim all --rebuild                    rebuild all providers
  reclaim --version                        print version
  reclaim import kept VAULT [--providers kimi,claude] [-o DIR]
  reclaim import chatgpt CONVERSATIONS.json [-o DIR]
  reclaim import scrapemychats EXPORT_DIR [-o DIR]
  reclaim parse  googleaistudio --from-folder DIR [--titles MAP.json] [-o DIR]
  reclaim export haevn-md . archive.zip

Providers: googleaistudio, deepseek, kimi, chatgpt (all = every provider).
Common options: --skip N, --limit N, --dry-run, --no-raw, -o/--output-dir.
Output has exactly two levels: default = essential info + summary only;
--log = full per-chat log + progress/ETA (failures always print).

Examples:
  reclaim googleaistudio                  # update AI Studio (new + changed)
  reclaim all --rebuild                   # re-archive everything, all providers
  reclaim deepseek "latex"                # re-fetch chats with "latex" in title
  reclaim kimi --list                     # print chat titles, no download
  reclaim chatgpt --dry-run               # preview, nothing downloaded
  reclaim status                          # offline archive overview
"""

from __future__ import annotations

import sys
import traceback

PROVIDERS = {
    'googleaistudio': 'reclaim.providers.googleaistudio',
    'deepseek': 'reclaim.providers.deepseek',
    'kimi': 'reclaim.providers.kimi',
    'chatgpt': 'reclaim.providers.chatgpt',
}
ALL_PROVIDERS = list(PROVIDERS)
PARSERS = {
    'googleaistudio': 'reclaim.providers.aistudio_files',
}
IMPORTERS = {
    'kept': 'reclaim.providers.kept_vault',
    'chatgpt': 'reclaim.providers.chatgpt_import',
    'scrapemychats': 'reclaim.providers.chatgpt_import',
}

USAGE = __doc__


def _run_all(rest: list[str]) -> int:
    """Run every configured provider with the same args in ONE browser session.

    Providers are filtered by `.reclaim.json` (see core/config.py); skipped
    providers never open a browser window or wait for a login. A provider's
    failure is reported and the run continues with the next.
    """
    if rest and rest[0] in ('-h', '--help'):
        print(USAGE)
        return 0
    # Validate args once up front — argparse exits before any browser launch.
    first = __import__(PROVIDERS[ALL_PROVIDERS[0]], fromlist=['parse_args'])
    first.parse_args(list(rest))

    from pathlib import Path
    from .core import browser, config
    root = Path(__file__).resolve().parents[1]
    chosen, skipped, unknown = config.selected_providers(root, ALL_PROVIDERS)
    for prov in skipped:
        print(f'  {prov}: skipped (not in {config.CONFIG_NAME} providers)')
    for prov in unknown:
        print(f'  warning: {config.CONFIG_NAME} lists unknown provider '
              f'"{prov}" (known: {", ".join(ALL_PROVIDERS)})')
    if not chosen:
        print('nothing to do — no providers selected')
        return 1

    from playwright.sync_api import sync_playwright
    codes: list[tuple[str, int]] = []
    with sync_playwright() as p:
        ctx, page = browser.launch(p)
        try:
            for prov in chosen:
                print(f'\n========== {prov} ==========')
                mod = __import__(PROVIDERS[prov],
                                 fromlist=['parse_args', 'run_session'])
                try:
                    args = mod.parse_args(list(rest))
                    codes.append((prov, mod.run_session(page, args)))
                except Exception:
                    traceback.print_exc()
                    codes.append((prov, 2))
        finally:
            ctx.close()
    print('\n===== summary =====')
    for prov, code in codes:
        status = 'ok' if code == 0 else f'exit {code}'
        print(f'  {prov}: {status}')
    return 0 if all(code == 0 for _, code in codes) else 2


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ('-h', '--help'):
        print(USAGE)
        return 0
    if argv[0] == '--version':
        from importlib.metadata import PackageNotFoundError, version
        try:
            print(f'reclaim {version("reclaimmychats")}')
        except PackageNotFoundError:
            print('reclaim (dev, not installed)')
        return 0
    cmd, rest = argv[0], argv[1:]

    if cmd in PROVIDERS:
        mod = __import__(PROVIDERS[cmd], fromlist=['main'])
        return mod.main(rest)
    if cmd == 'all':
        return _run_all(rest)
    if cmd == 'status':
        mod = __import__('reclaim.core.status', fromlist=['main'])
        return mod.main(rest)
    if cmd == 'parse' and rest and rest[0] in PARSERS:
        mod = __import__(PARSERS[rest[0]], fromlist=['main'])
        return mod.main(rest[1:])
    if cmd == 'import' and rest and rest[0] in IMPORTERS:
        mod = __import__(IMPORTERS[rest[0]], fromlist=['main'])
        if rest[0] in ('chatgpt', 'scrapemychats'):
            return mod.main(rest)
        return mod.main(rest[1:])
    if cmd == 'export' and rest and rest[0] == 'haevn-md':
        mod = __import__('reclaim.exporters.haevn_md', fromlist=['main'])
        return mod.main(rest[1:])

    print(f'unknown command: {" ".join(argv)}\n')
    print(USAGE)
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
