"""ReclaimMyChats unified CLI.

Usage:
  reclaim <provider> [TITLE] [options]     update: new + changed chats only
  reclaim <provider> --rebuild [options]   everything, freshly (overwrite)
  reclaim <provider> "latex"               chats whose title contains "latex"
  reclaim <provider> --url URL             one exact chat
  reclaim <provider> --list [TITLE]        print chat titles, no download
  reclaim <provider> --log [options]       verbose progress + timings
  reclaim <provider> --dry-run [options]   preview what would be fetched
  reclaim status [-o DIR]                  offline database overview
  reclaim init [DIR]                       create a database (default: the configured folder)
  reclaim unlock [--force]                 remove a lock left by a run that has ended
  reclaim gui                              launch the desktop application
  reclaim all [options]                    update all six providers, in order
  reclaim all --rebuild                    rebuild all providers
  reclaim --version                        print version
  reclaim import kept VAULT [--providers kimi,claude] [-o DIR]
  reclaim import chatgpt CONVERSATIONS.json [-o DIR]
  reclaim import scrapemychats EXPORT_DIR [-o DIR]
  reclaim parse  googleaistudio --from-folder DIR [--titles MAP.json] [-o DIR]
  reclaim export haevn-md . chats.zip

Providers: googleaistudio, deepseek, kimi, chatgpt, claude, googlegemini
(all = every provider). Common options: --skip N, --limit N, --dry-run,
--no-raw, -o/--output-dir.
Output has exactly two levels: default = essential info + summary only;
--log = full per-chat log + progress/ETA (failures always print).

Examples:
  reclaim googleaistudio                  # update AI Studio (new + changed)
  reclaim all --rebuild                   # fetch everything again, all providers
  reclaim deepseek "latex"                # re-fetch chats with "latex" in title
  reclaim kimi --list                     # print chat titles, no download
  reclaim chatgpt --dry-run               # preview, nothing downloaded
  reclaim claude --dry-run                # preview Claude, nothing downloaded
  reclaim googlegemini --list             # list Gemini chat titles
  reclaim status                          # offline database overview
"""

from __future__ import annotations

import sys

PROVIDERS = {
    'googleaistudio': 'reclaim.providers.googleaistudio',
    'deepseek': 'reclaim.providers.deepseek',
    'kimi': 'reclaim.providers.kimi',
    'chatgpt': 'reclaim.providers.chatgpt',
    'claude': 'reclaim.providers.claude',
    'googlegemini': 'reclaim.providers.googlegemini',
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
    from .core import config, service
    service.provider_module(ALL_PROVIDERS[0]).parse_args(list(rest))
    root = config.default_root()
    chosen, skipped, unknown = config.selected_providers(root, ALL_PROVIDERS)
    for prov in skipped:
        print(f'  {prov}: skipped (not in {config.CONFIG_NAME} providers)')
    for prov in unknown:
        print(f'  warning: {config.CONFIG_NAME} lists unknown provider '
              f'"{prov}" (known: {", ".join(ALL_PROVIDERS)})')
    if not chosen:
        print('nothing to do — no providers selected')
        return 1

    summary = service.ArchiveController(root).run_cli(chosen, rest)
    print('\n===== summary =====')
    for prov, code in summary.codes:
        status = 'ok' if code == 0 else f'exit {code}'
        print(f'  {prov}: {status}')
    return summary.exit_code


def _run_single(name: str, rest: list[str]) -> int:
    """One provider through the shared controller, keeping its exit code."""
    from .core import config, service
    summary = service.ArchiveController(config.default_root()).run_cli([name], rest, headers=False)
    return summary.codes[0][1] if summary.codes else 2


def _init(rest: list[str]) -> int:
    import argparse
    from pathlib import Path
    from .core import config, database
    ap = argparse.ArgumentParser(
        prog='reclaim init',
        description='Create a database in DIR, or mark an existing one made by an earlier '
                    'version. Without DIR, use the configured database folder.')
    ap.add_argument('dir', nargs='?', help='folder for the database')
    args = ap.parse_args(rest)
    root = config.default_root()
    configured = config.archive_root(root)
    path = Path(args.dir).expanduser().resolve() if args.dir else configured
    before = database.inspect(path).kind
    database.init(path)
    print({'ready': f'{path} is already a Reclaim My Chats database.',
           'legacy': f'Marked the existing database in {path}.'}.get(
               before, f'Created a new database in {path}.'))
    if path != configured:
        print('To use it, choose it as the database folder in Settings, or set "archive" '
              f'in {config.CONFIG_NAME}.')
    return 0


def _unlock(rest: list[str]) -> int:
    import argparse
    from .core import config, locks
    ap = argparse.ArgumentParser(
        prog='reclaim unlock',
        description='Remove the locks of the database and the login data folder, left '
                    'by a run that has ended (for example after a crash).')
    ap.add_argument('--force', action='store_true',
                    help='also remove a lock held by a process still running on this computer')
    args = ap.parse_args(rest)
    root = config.default_root()
    code = 0
    for folder in (config.archive_root(root), config.profile_root(root)):
        holder = locks.read_holder(folder)
        if holder is None:
            print(f'{folder}: not locked')
        elif locks.running_here(holder) and not args.force:
            print(f'{folder}: locked by process {holder.get("pid")} on this computer, which '
                  'is still running. Close it, or use --force.')
            code = 1
        else:
            locks.remove(folder)
            print(f'{folder}: removed the lock held by {holder.get("host") or "an unknown computer"} '
                  f'since {holder.get("started") or "an unknown time"}')
    return code


def _hint(exc: Exception) -> str:
    from .core import database, locks
    if isinstance(exc, database.DatabaseError) and exc.kind in ('missing', 'empty'):
        return 'To create a new database there, run: reclaim init'
    if isinstance(exc, locks.LockedError) and exc.removable:
        return 'To remove the lock, run: reclaim unlock'
    return ''


def main(argv=None):
    from .core import config, database, locks
    try:
        return _dispatch(argv)
    except (config.SettingsError, database.DatabaseError, locks.LockedError) as exc:
        print(f'error: {exc}', file=sys.stderr)
        hint = _hint(exc)
        if hint:
            print(hint, file=sys.stderr)
        return 2


def _dispatch(argv=None):
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
        return _run_single(cmd, rest)
    if cmd == 'init':
        return _init(rest)
    if cmd == 'unlock':
        return _unlock(rest)
    if cmd == 'all':
        return _run_all(rest)
    if cmd == 'gui':
        if rest:
            print('usage: reclaim gui')
            return 2
        from .gui import main as gui_main
        return gui_main()
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
