# Contributing

Thanks for considering a contribution to ReclaimMyChats!

## Ground rules

- **No chat data in PRs.** Never commit provider output, browser profiles,
  `.last_sync_*.json` files, or anything scraped — `.gitignore` covers all
  of it; keep it that way.
- **No credentials anywhere.** No tokens, cookies, or secrets in code,
  logs, or docs. Reference them by name only.
- **Privacy of the archive is the point.** Changes that weaken the
  git-ignore boundary or the media-stripping in `raw.json` will be rejected.

## Before opening a PR

```bash
./scripts/run_tests.sh  # offline tests must pass
ruff check reclaim/ tests/
pyright reclaim/ tests/
```

Use the newest installed Python (the scripts pick it automatically).

## Code of conduct

Be respectful; this is a small project but it follows the usual
open-source norms. Contributions are welcome whether they are bug reports,
endpoint research, docs, or code.
