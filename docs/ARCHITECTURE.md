# Architecture

Reclaim My Chats has two frontends over one set of native provider modules.
Both write the same on-disk database format.

```text
reclaim CLI ───────┐
                   ├── ArchiveController ─┬─ shared Playwright session
Flet desktop GUI ──┘                      │   ├── ChatGPT, Claude, Gemini
                                          │   └── Google AI Studio, DeepSeek, Kimi
                                          ├─ Google Antigravity (files on this computer)
                                          └─ writer, sync state, run manifests

Offline import and export commands ─────── importers/exporters ── database
```

`reclaim/core/service.py` parses provider arguments and runs selected native
providers in one browser session. Every run goes through this controller:
the GUI, `reclaim all` and single-provider commands. Before a browser opens
it validates the settings (`config.SettingsError`), checks the database
folder (`core/database.py`: the `.reclaim-database.json` marker, adopting
older databases), takes lock files on the login data folder and the
database (`core/locks.py`), and opens the run log (`core/logs.py`). A
provider whose class sets `uses_browser = False` reads files on this
computer: Google Antigravity (`providers/antigravity.py`). It is handed no
page, a run of only such providers takes no lock on the login data folder,
and the browser opens when the first provider that needs it starts
(`service._Browser`); such a provider has no responses to capture, so
`capture` refuses it. Imports and the offline AI Studio parser use the same
checks through `offline_session`. An in-process lock also stops concurrent
GUI clicks.

Every provider runs on `reclaim/core/engine.py`. A provider class supplies
only what is specific to its service: `open` (log in and return a session),
`listing`, `from_url` and `fetch`. The engine owns the command-line options,
the title, `--skip` and `--limit` filters, `--list`, `--dry-run`, skipping
unchanged chats, the download loop with its progress events, saving, the
run manifest and the exit code. Requests made from the page go
through `reclaim/core/retry.py`, one policy for all providers: 401/403 stop
at once, 429 waits as long as `Retry-After` asks, server errors and failed
requests are retried briefly. A request that fails for good raises an error
that carries the HTTP status of the last response (`retry.RequestFailed`), and
a provider decides a chat is deleted only from that status being 404
(`retry.is_not_found`), never from the error text. DeepSeek is the one
exception: it answers HTTP 200 for a chat that does not exist, with business
code 1 and the message "invalid chat session id" in the reply, and only that
exact answer counts (`deepseek._is_missing_chat`). Antigravity has no
requests: a conversation is deleted when its file is gone from Antigravity's
folder.

Antigravity keeps each conversation as a SQLite file whose `steps` table holds
one protobuf message per step. `providers/antigravity_steps.py` reads that
wire format directly (no protobuf library), using only the field numbers it
needs, taken from the schema embedded in Antigravity's `agy` program; a step
that does not parse stops that chat with a message instead of saving it
incomplete. See [UPSTREAM_REVIEW.md](UPSTREAM_REVIEW.md#google-antigravity)
for the layout.

Provider modules acquire conversations from each service, normalize them to
the models in `reclaim/core/model.py`, and the engine saves each chat with
`manifest.save_chat`: `reclaim/core/writer.py` writes the folder (Markdown,
`chat.json`, media), then `raw.json`, then the sync record. All files are
written atomically (`core/fsutil.py`). Sync records classify chats as new,
changed, restored, or unchanged; they are saved after every chat and rebuilt from the
`chat.json` files when missing or damaged. `reclaim/core/lifecycle.py` plans
and applies moves between active folders, `_Archived/`, and `_Deleted/` with
completeness checks, absence confirmation, a mass-change brake, and
`.reclaim_events.jsonl` logging; `reclaim/core/tidy.py` retires safe duplicate
folders, by hand (`reclaim tidy`) or, with the `retire_older_copies` setting
on, after an update of every chat (`engine._replace_older_copies`). A run
reports what it filed away and replaced as `moved` and `replaced` events.
`reclaim/core/manifest.py` also records each run.

`reclaim/core/autostart.py` manages per-user login autostart entries and
desktop completion notifications. Progress is reported only as events (`core/events.py`, which lists the
kinds): the engine, the providers, the login helpers and the sync records
emit a `ProgressEvent` and never print. The controller decides where a run's
events go and sets that as the thread's route, so code deep in a provider
needs no extra parameter. `core/render.py` renders them once per front end:
`TerminalRenderer` for commands (a line per chat and tracebacks only with
`--log`), `LogRenderer` for the log file (outcomes, warnings, and errors with
their tracebacks), and the window's `_event`. The log and the window's
Details share one wording, `render.describe`. Anything else printed during a
run, such as a library warning, still reaches the log. The Flet GUI receives
events from a worker thread through an asyncio queue, then updates controls
on its own event loop. Playwright objects belong to the worker thread that
launched the browser, so the window never calls them directly: its *Bring
browser forward* button only sets a flag (`browser.bring_browser_forward`),
which the shared login waits (`wait_for_login`, `interactive_login`) act on
from the worker thread within a fraction of a second. Status is read from local manifests, chat files, and `.reclaim_checks.json`
(recorded when a provider check or update succeeds without modifying the database)
on another worker thread. Startup checks for a newer GitHub release non-blockingly
(`core/releases.py`), and never guesses whether a remote chat changed; dry run or
update must contact the provider to know that.

`core/capture.py` turns a provider's real responses into a file that can be
shared and kept as a test fixture: each letter and digit of the chats is
replaced, while the structure, field names, numbers, dates and the words the
parsers look for stay. Those words come from the provider code itself (its
string constants, read from the loaded modules). Equal strings get equal
replacements, so ids and citation markers still match up. `retry.recording()`
records non-auth responses during `engine.capture` (invoked via
`ArchiveController.capture` from `reclaim capture` and *Capture a chat* in
Diagnostics).

Settings, database, and browser profile paths share one writable root. A Git
checkout keeps its original root. Installed or frozen applications use the
operating system's user data directory. `.reclaim.json` uses a versioned
schema (`SETTINGS_VERSION = 2`) with automatic migration for older keys.
`RECLAIM_HOME`, `RECLAIM_KIMI_URL`, `RECLAIM_CHROME_PATH`, and
`RECLAIM_LOGIN_TIMEOUT` act as environment overrides over `.reclaim.json`;
the `archive` and `profile` keys can choose other paths.
The profile contains browser sessions and is never part of a release asset.

The desktop package is made with `flet pack` and PyInstaller. The native
provider imports in `core/service.py` are explicit so PyInstaller can include
them. Chrome or Chromium comes from the user's system, or from an existing
Playwright browser installation; it is not bundled in the desktop download.
`browser.find_chrome` looks for the system browser and
`browser.find_playwright_chromium` for Playwright's cached one; `reclaim
doctor` and the setup wizard report whichever a run would use.
Flet's `flet test` uses a different Flutter host, so a separate packaged-app
smoke test is part of release verification.

The CLI additionally supports offline Google AI Studio folder parsing,
ChatGPT and Kept imports, and HAEVN Markdown export. These modes use the
same writer and database format without launching a browser.
