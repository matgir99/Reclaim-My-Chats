# Agents guide for Reclaim My Chats

Reclaim My Chats saves conversations from ChatGPT, Claude, Google Gemini, Google AI Studio, DeepSeek, and Kimi into a local folder of Markdown, JSON, and media called the **database**. Two front ends share one controller: a Flet desktop window (`reclaim/gui.py`) and a command line (`reclaim/__main__.py`).

Read in this order:

1. [`docs/ROADMAP.md`](docs/ROADMAP.md) — active plan (Phases 0–5) and completed work.
2. [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — how the engine, controller, providers, and UI fit together.
3. [`README.md`](README.md) — user-facing commands, installation, and settings.
4. [`docs/OUTPUT.md`](docs/OUTPUT.md) — database layout that users' saved chats depend on.
5. [`docs/MAINTAINING.md`](docs/MAINTAINING.md) and [`docs/RELEASING.md`](docs/RELEASING.md) — branches, checks, fixtures, and releases.
6. [`docs/GUI.md`](docs/GUI.md) — the desktop window and its tests.
7. [`docs/UPSTREAM_REVIEW.md`](docs/UPSTREAM_REVIEW.md) — provider formats and upstream reference projects.
8. [`docs/CLI_REDESIGN.md`](docs/CLI_REDESIGN.md) — CLI contract and offline modes.

If local files exist in `docs/status_and_plan/` from August 2026 (`v3.0.0`), they are out of date; do not plan from them.

## Rules

1. **The window comes first.** Reclaim My Chats is a desktop application first and a CLI second. A user-facing feature is finished only when it works in the desktop window (`reclaim/gui.py`) as well as the CLI (`reclaim/__main__.py`).
2. **Use "database", not "archive".** Call the local folder where chats are saved **the database** ("saved", "in the database") in user-facing copy and docs. Reserve **archived** for a provider's own archive feature, and **deleted** for chats removed at the provider. Internal code and config identifiers (`archive_root`, `ArchiveController`, and the `"archive"` key in `.reclaim.json`) keep their names for compatibility.
3. **Never delete user data.** Updates add, replace, or move files. Removing or overwriting anything in a real database requires the owner's explicit approval each time.
4. **No private data in the repository.** Never put real chats, raw provider responses, browser profiles, cookies, tokens, logs, personal file paths, or email addresses in commits, PRs, issues, or test fixtures. Never send the owner's email address to any external service. Test fixtures must be synthetic, or cleaned with `reclaim/core/capture.py` and checked by eye.
5. **The owner merges.** Work on focused feature branches off `develop`, push them, and open small, self-contained PRs into `develop` following [`.github/PULL_REQUEST_TEMPLATE.md`](.github/PULL_REQUEST_TEMPLATE.md). Do not merge PRs, post GitHub comments or review resolutions, or open a `develop` -> `main` release PR unless asked.
6. **Fail loudly.** Invalid `.reclaim.json` settings, a missing database, a held lock, or a changed provider response format must stop with a clear message saying what happened and how to fix it, in both the window and the CLI. Never fall back silently.

## Environment and safety

- Run Python commands from the project `.venv` (`.venv/bin/python`, `.venv/bin/reclaim`). Ignore any stale global `~/.local/bin/reclaim` install.
- Treat `chats/` and `.playwright-profile/` in a local checkout as live user data: use them only for read-only commands (`reclaim status`, `reclaim doctor`, `--dry-run`, `--list`) unless the owner asks for more, and point `RECLAIM_HOME` at a temporary directory for any write experiment.
- Live provider runs (`reclaim <provider>`, `reclaim all`, `reclaim doctor --live`, `reclaim capture`) launch Chrome and may wait for logins; only run them with the owner present.
- When inspecting or scripting the app window on Linux (`wmctrl`, `xprop`, screenshots), match the exact title `"Reclaim My Chats"` and verify `WM_CLASS` is `com.appveyor.flet` (or run with an isolated `RECLAIM_HOME` and target that window ID).

## Checks

Run these before committing or opening a PR (matching CI):

```bash
.venv/bin/python -m unittest discover -s tests -q
.venv/bin/python -m unittest discover -s tests -p browser_protocols.py
.venv/bin/ruff check reclaim tests scripts flet_integration
pyright --pythonpath .venv/bin/python reclaim tests scripts flet_integration
```

- Always use `unittest`, never `pytest` (pytest collects `flet_integration/`, which requires Flutter and a display harness).
- Ruff enforces a maximum function complexity of 12; split functions into named steps rather than raising the limit.
- Dependencies are pinned in `uv.lock`. After changing `pyproject.toml`, run `uv lock` and commit both files.
- `.gitignore` ignores `.github/*` except explicitly allowlisted entries (`!...`). Any new workflow or folder under `.github/` needs its own allowlist line.
- Commit subjects should be plain imperative sentences with no conventional-commit prefixes (e.g., `Add diagnostics: reclaim doctor and the Diagnostics panel`).

## Code map

| Path | Responsibility |
|:--|:--|
| `reclaim/core/service.py` | `ArchiveController` (`run`, `run_cli`, `capture`, `_execute`) shared by GUI and CLI. |
| `reclaim/core/engine.py` | Shared provider engine (`sync`, `check`, `capture`). |
| `reclaim/core/retry.py` | `send()` request policy and `recording()` capture recorder. |
| `reclaim/core/events.py`, `render.py`, `progress.py` | `ProgressEvent` routing (`routed()`) and terminal/log/GUI rendering. |
| `reclaim/core/database.py`, `locks.py`, `config.py`, `logs.py`, `status.py` | Database marker, lock files, `.reclaim.json` settings validation, run logs, offline status. |
| `reclaim/core/writer.py`, `manifest.py`, `model.py`, `fsutil.py`, `batchexecute.py`, `browser.py` | Atomic chat folder writing, sync records, chat data model, helpers, browser discovery/profile. |
| `reclaim/core/doctor.py`, `capture.py` | Offline/live diagnostics (`reclaim doctor [--live]`) and response sanitization/capture bundles (`reclaim capture`). |
| `reclaim/providers/` | Native providers (`chatgpt.py`, `chatgpt_references.py`, `claude.py`, `deepseek.py`, `googleaistudio.py`, `googlegemini.py`, `kimi.py`) and offline importers (`chatgpt_import.py`, `kept_vault.py`, `aistudio_files.py`). |
| `reclaim/gui.py`, `reclaim/__main__.py` | Flet desktop window and CLI entry point. |
| `tests/`, `flet_integration/`, `scripts/` | Offline unit/browser-protocol suites, Flet device integration suite, and release/maintenance scripts. |
