# Legacy standalone scrapers

These are the original single-file scrapers written before the unified
`reclaim/` package existed (v1.0.0). They work and are preserved for
reference, but **new work should use the `reclaim` package**:

```bash
python -m reclaim <googleaistudio|deepseek|kimi|chatgpt> [TITLE] [options]
```

| File | What it was |
|---|---|
| `scrape_googleaistudio.py` | AI Studio RPC replay scraper (v2, single-file). Superseded by `reclaim/providers/googleaistudio.py` |
| `scrape_deepseek.py` | DeepSeek IndexedDB scraper. Superseded by `reclaim/providers/deepseek.py` |
| `scrape_googleaistudio.md` / `scrape_deepseek.md` | Their original documentation |

Notes for anyone who wants to run them anyway: they expect a Playwright
persistent context at `.playwright-profile/` (same as the main package)
and the credentials/login flow described in the main README. They write
output next to the profile, so run them from a scratch directory, not a
clone of this repo (output folders are git-ignored anyway).
