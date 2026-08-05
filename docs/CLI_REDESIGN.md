# CLI Redesign — Implementation Handoff

**Status:** approved design, not yet implemented. Execute top to bottom, then run the gates and commit.

## 1. Context

ReclaimMyChats archives a user's AI chat history (Google AI Studio, DeepSeek, Kimi, ChatGPT) to local folders. The current CLI is `reclaim scrape <provider> [--resume|--only|...]`. The owner finds these names meaningless (`--resume` sounds like "continue an interrupted run"; `--only` doesn't say *only what*). The redesign makes every word convey meaning. The tool name `reclaim` is itself the verb, so `scrape`/`retrieve` disappear entirely.

- Repo: `/home/matgir99/Desktop/PROGETTI/ReclaimMyChats` (public, github.com/matgir99/ReclaimMyChats, v2.0.0)
- Python: **always the newest installed** — `scripts/py.sh` (`pick_python`) picks it; currently python3.14. Never hardcode `python3.12`.
- Console entry point: `reclaim` (pyproject.toml) → `reclaim/__main__.py`
- Tests are **offline only** (parser/model/writer level). Do not run live scrapes; they need the browser profile + logins and take 20+ min.

## 2. Agreed design (the target surface)

```
reclaim <provider>                      # update (default): new + changed chats only, fast
reclaim <provider> --rebuild            # rebuild: everything, freshly, overwrite local copies
reclaim <provider> "latex"              # chats whose title contains "latex", fetched freshly
reclaim <provider> --url URL            # one exact chat
reclaim <provider> --list               # print chat titles, no download
reclaim <provider> --log                # verbose full logs (compact progress is ALWAYS on)
reclaim <provider> --dry-run            # preview what update/rebuild WOULD fetch, no download
reclaim status                          # offline archive overview: counts, last sync, failures
reclaim all                             # update all four providers, sequentially
reclaim all --rebuild                   # rebuild all providers
```

Providers: `googleaistudio`, `deepseek`, `kimi`, `chatgpt` (`all` = pseudo-provider). Remaining flags: `--skip N`, `--limit N`, `--dry-run`, `--no-raw`, `-o/--output-dir DIR`. `import` and `export` subcommands stay unchanged.

**Removed completely (no aliases, no back-compat — explicit owner decision):**
the `scrape` subcommand, `--resume`, `--only`, `--start`, `--retrieve`, `--update`, `--match`, `--incremental`, `--fresh`, `--keep-raw`.

**Semantics:**

| Input | Selection | Freshness |
|---|---|---|
| no title, no `--rebuild` | all chats | **update**: skip chats whose server `updated_at` matches the local sync record; fetch new + changed |
| `--rebuild` | all chats | fetch everything, **overwrite** local files |
| `TITLE` positional | title contains TITLE (case-insensitive) | always fetch matches freshly (overwrite) — naming a chat means "get it now" |
| `--url` | that one chat | fetch, overwrite |
| `TITLE` + `--url` | — | **error** (`parser.error`, exit 2) |
| `--list` | (title filter allowed) | no download; print numbered titles + count, exit 0 |
| `--dry-run` | (combines with every mode above) | nothing downloaded or written; print what *would* happen |

`reclaim all` dispatches each provider with the same args, sequentially; exit 0 only if all succeed, else 2.

## 3. What already exists — reuse, don't rebuild

- **Change detection is implemented.** `reclaim/core/manifest.py` → `SyncState`: per-provider `.last_sync_<provider>.json`, `is_unchanged(chat_id, updated_at)` (timestamp equality; `None` timestamp → presence-based fallback), `mark()`, `save()`.
- **All four providers already collect `updated_at`** in their listing step and build `updated_map` in `main()`: aistudio `list_prompts` (id/title/updated_at), deepseek history list, kimi `updateTime`, chatgpt `update_time`.
- Providers' `run()` loops already skip unchanged when `resume=True` — the mechanism becomes the default update mode.
- `write_manifest` (run manifests), `write_chat`/`write_raw` (output), core model — all stay.

## 4. Work items (in order)

1. **Rename module** `reclaim/providers/aistudio.py` → `googleaistudio.py`; `PROVIDER = 'googleaistudio'`; default output dir string `'Google AI Studio'` **stays**. Update every import (`__main__.py`, `tests/`, anything else — grep for `providers.aistudio`, `providers import aistudio`).
2. **SyncState migration:** in `SyncState.__init__`, if `<out>/.last_sync_googleaistudio.json` is missing but `<out>/.last_sync_aistudio.json` exists, rename it (one-time; prevents re-downloading 71 chats on first update run).
3. **Rewrite `reclaim/__main__.py` dispatch:** first positional = provider or `all`; `import`/`export` unchanged; `--help` prints the surface above with the examples.
4. **Rewrite each provider `main()` argparse** (googleaistudio, deepseek, kimi, chatgpt):
   - positional `title` (`nargs='?'`), `--rebuild`, `--url`, `--list`, `--log`, `--dry-run`, `--skip` (replaces `--start`), `--limit`, `--no-raw` (make uniform across all four), `-o/--output-dir`
   - conflict check: `title` + `--url` → `parser.error('pass a title or --url, not both')`
   - delete `--resume`, `--only`, `--start`, `--keep-raw`
5. **Rewire each provider `main()` logic:**
   - build listing as today; `--list` → print `N. title` lines + total, return 0 (after login, no scraping)
   - apply title filter → then `--skip`/`--limit` slices
   - compute `skip_unchanged = not args.rebuild and not title` (title/url imply fresh fetch); pass to `run()` (rename the `resume` parameter to `skip_unchanged` for clarity)
   - `--dry-run`: after computing selection + freshness, print `would fetch: N (M new, K changed) · would skip: J unchanged` plus the affected titles; return 0 **without scraping or writing anything** (login + listing only)
   - no matches → `No chats matched.`, return 1
6. **Overwrite bug fix in `reclaim/core/writer.py` (`write_chat`):** today an existing `<slug>.md` triggers dedup to `<slug>_1.md` — re-retrieval must overwrite instead:
   - if `chat_dir/chat.json` exists and its `id` == the incoming chat's id: write to `<slug>.md` (overwrite); delete any *other* `.md` files in that dir (stale slug from a renamed title); overwrite `chat.json`/`raw.json`; media stays additive
   - if the id differs (genuinely different chat colliding on title) → keep current `_1` dedup
7. **Progress/logging:** compact per-chat line is **always printed** (normalize the existing prints to `[i/N] Title -> Nt, N,NNN chars[, N img][, N doc]`). Add `--log` verbose layer: `[i/N] NN% · elapsed M:SS · ETA M:SS` plus per-chat detail (path used, timings). Small helper in `reclaim/core/progress.py`, e.g. `progress(i, n, t0) -> str`; wire into all four `run()` loops via a `log: bool = False` param.
8. **`reclaim all`:** in `__main__.py`, ONE shared browser session for all providers: each provider exposes `parse_args()` + `run_session(page, args)`; `_run_all` launches the browser once, validates args up front, isolates per-provider failures (trace + exit 2, continue), prints a one-line-per-provider summary.
9. **`reclaim status`:** fully offline (no browser/login) archive overview — read each provider dir's latest `.reclaim_manifest.json` + `.last_sync_<provider>.json`: chats archived, last sync time + duration, failures in the last run, totals (images/chars); missing dir → `not archived yet`. Implement in `reclaim/core/status.py` (pure file reading, unit-testable), wire as a `status` subcommand in `__main__.py`; `-o` overrides the scan root.
10. **Update `run.sh`:** usage becomes `./run.sh <googleaistudio|deepseek|kimi|chatgpt|all> [args...] | progress | stop`, forwarding to `python -m reclaim "$PROVIDER" "$@"`; update the header comment + usage examples. (This also closes the open STATUS.md item "extend run.sh to kimi/chatgpt".)
11. **Docs:** rewrite README usage/Quickstart sections to the new surface, including `status` and `--dry-run` (keep install/platform sections). Update `docs/STATUS.md` CLI references + tick the run.sh item. **Do NOT edit `docs/PLAN.md`** (historical master plan; checkbox-only rule).
12. **Tests** (`tests/`, all offline):
    - fix imports after the module rename
    - arg parsing: title positional, `--rebuild`, `--list`, `--log`, conflict error, removed flags rejected (`SystemExit`), `--skip` default 0
    - writer overwrite: same id → single overwritten md, no `_1`; different id → dedup preserved
    - SyncState: legacy `.last_sync_aistudio.json` migration; `is_unchanged` timestamp semantics
    - dry-run: correct new/changed/unchanged counts against a fake sync state; performs no writes
    - status: aggregation from fixture manifests/sync-state files (counts, last-sync, failures)
    - run `./scripts/run_tests.sh` (auto-picks python3.14)
13. **Gates:** `python -m pyright` 0 errors · `ruff check` clean · all tests green. Then one commit + push. Suggest tagging **v3.0.0** (breaking CLI change) but leave tagging to the owner.

## 5. Decisions / exclusions

- **`--dry-run` semantics:** login + listing only, never scrapes or writes; prints `would fetch: N (M new, K changed) · would skip: J unchanged` plus the affected titles; combines with every selection mode (title, `--url`, `--rebuild`, `--skip/--limit`).
- **`reclaim status` is fully offline** — reads manifest/sync-state files only, never launches the browser.
- No short aliases for providers (owner wants full names; `googlegemini` is planned later as a separate provider).
- First-ever run has no sync state → update mode naturally downloads everything (= rebuild), so the default is safe for new users.
- `updated_at=None` listings keep the presence-based skip (existing `SyncState` behavior).
- Multiple title matches → fetch all of them. None → exit 1 with `No chats matched.`

## 6. Rules (non-negotiable)

- Gitignored, never commit: provider output dirs (`Google AI Studio/`, `Deepseek Chat/`, `Kimi Chat/`, `ChatGPT/`, `output/`), `.playwright-profile/`, `dev-probes/`, manifests, `*.log`, `.env*`.
- Never print tokens/cookies; browser profile holds live sessions.
- Script-first: real `.py` files writing to files; `python -c` only for sub-second read-only checks.
- Use the `todo` tool for the work items above; exactly one in_progress at a time.
- If live verification is ever needed (optional): `pkill -f playwright-profile`, `rm -f .playwright-profile/Singleton*` first; never log out of the profile.

## 7. File map

| File | Role |
|---|---|
| `reclaim/__main__.py` | CLI dispatch |
| `reclaim/providers/googleaistudio.py` | rename from `aistudio.py`; `list_prompts`, `run`, `main` |
| `reclaim/providers/{deepseek,kimi,chatgpt}.py` | same `run`/`main` rework |
| `reclaim/core/manifest.py` | `SyncState` (+migration), `write_manifest` |
| `reclaim/core/writer.py` | `write_chat` overwrite fix, `write_raw` |
| `reclaim/core/progress.py` | new tiny helper |
| `reclaim/core/status.py` | new: offline archive overview for `reclaim status` |
| `run.sh` | new syntax, all providers |
| `tests/test_parsers.py` + new test file(s) | offline tests |
| `README.md`, `docs/STATUS.md` | docs update |

## 8. Amendments (owner-approved, post-implementation)

- **Output has exactly two levels, no in between:**
  - *Default* — essential info + summary only: real runs print the `N chats`
    header and the `Done: X ok, Y failed` summary (failures always print);
    `--dry-run` prints the `would fetch: N (M new, K changed) · would skip:
    J unchanged` counts line plus the affected titles.
  - *`--log`* — the full log: per-chat lines (`[i/N] Title -> Nt, N,NNN
    chars` / `-> skip (unchanged)`), verbose progress (`[i/N] NN% · elapsed
    M:SS · ETA M:SS`) and per-chat detail (path/files/timings).
    `--dry-run --log` prints per-chat `fetch (new|changed|fresh)` /
    `skip (unchanged)` lines before the counts line.
- `-q/--quiet` was added in an earlier revision and **removed again** as
  redundant — quiet is the default, so there is nothing to opt into.
