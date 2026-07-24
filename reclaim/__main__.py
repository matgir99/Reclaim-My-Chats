"""ReclaimMyChats unified CLI.

Usage:
  python -m reclaim scrape aistudio [--url X] [--only X] [--start N] [--limit N]
                                    [--resume] [--keep-raw] [-o DIR]
  python -m reclaim scrape deepseek [--url X] [--only X] [--resume] [-o DIR]
  python -m reclaim scrape kimi     [--url X] [--only X] [--resume] [-o DIR]
  python -m reclaim scrape chatgpt  [--url X] [--only X] [--resume] [-o DIR]
  python -m reclaim parse  aistudio --from-folder DIR [--titles MAP.json] [-o DIR]
  python -m reclaim import kept VAULT [--providers kimi,claude] [-o DIR]
  python -m reclaim import chatgpt CONVERSATIONS.json [-o DIR]
  python -m reclaim import scrapemychats EXPORT_DIR [-o DIR]

Each provider module owns its argparse; we dispatch with the remaining args.
"""

from __future__ import annotations

import sys

SCRAPERS = {
    'aistudio': 'reclaim.providers.aistudio',
    'deepseek': 'reclaim.providers.deepseek',
    'kimi': 'reclaim.providers.kimi',
    'chatgpt': 'reclaim.providers.chatgpt',
}
PARSERS = {
    'aistudio': 'reclaim.providers.aistudio_files',
}
IMPORTERS = {
    'kept': 'reclaim.providers.kept_vault',
    'chatgpt': 'reclaim.providers.chatgpt_import',
    'scrapemychats': 'reclaim.providers.chatgpt_import',
}

USAGE = __doc__


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ('-h', '--help'):
        print(USAGE)
        return 0
    action, target = argv[0], argv[1] if len(argv) > 1 else ''
    rest = argv[2:] if len(argv) > 1 else []

    if action == 'scrape' and target in SCRAPERS:
        mod = __import__(SCRAPERS[target], fromlist=['main'])
        return mod.main(rest)
    if action == 'parse' and target in PARSERS:
        mod = __import__(PARSERS[target], fromlist=['main'])
        return mod.main(rest)
    if action == 'import' and target in IMPORTERS:
        mod = __import__(IMPORTERS[target], fromlist=['main'])
        if target in ('chatgpt', 'scrapemychats'):
            return mod.main([target] + rest)
        return mod.main(rest)
    if action == 'export' and target == 'haevn-md':
        mod = __import__('reclaim.exporters.haevn_md', fromlist=['main'])
        return mod.main(rest)

    print(f'unknown command: {" ".join(argv)}\n')
    print(USAGE)
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
