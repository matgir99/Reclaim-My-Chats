# Architecture

Reclaim My Chats has two frontends over one set of native provider modules.
Both write the same on-disk database format.

```text
reclaim CLI ───────┐
                   ├── ArchiveController ── shared Playwright session
Flet desktop GUI ──┘                        ├── ChatGPT, Claude, Gemini
                                          ├── Google AI Studio, DeepSeek, Kimi
                                          └── writer, sync state, run manifests

Offline import and export commands ─────── importers/exporters ── database
```

`reclaim/core/service.py` parses provider arguments and runs selected native
providers in one browser session. Every run goes through this controller:
the GUI, `reclaim all` and single-provider commands. Before a browser opens
it validates the settings (`config.SettingsError`), checks the database
folder (`core/database.py`: the `.reclaim-database.json` marker, adopting
older databases), takes lock files on the login data folder and the
database (`core/locks.py`), and copies everything printed to the run log
(`core/logs.py`). Imports and the offline AI Studio parser use the same
checks through `offline_session`. An in-process lock also stops concurrent
GUI clicks.

Provider modules acquire conversations from each service, normalize them to
the models in `reclaim/core/model.py`, and save each chat with
`manifest.save_chat`: `reclaim/core/writer.py` writes the folder (Markdown,
`chat.json`, media), then `raw.json`, then the sync record. All files are
written atomically (`core/fsutil.py`). Sync records classify chats as new,
changed, or unchanged; they are saved after every chat and rebuilt from the
`chat.json` files when missing or damaged. `reclaim/core/manifest.py` also
records each run.

Providers emit `ProgressEvent` callbacks for run, provider, and chat
transitions. The Flet GUI receives these from a worker thread through an
asyncio queue, then updates controls on its own event loop. Database status is
read from local manifests and files on another worker thread. Startup never
guesses whether a remote chat changed; dry run or update must contact the
provider to know that.

Settings, database, and browser profile paths share one writable root. A Git
checkout keeps its original root. Installed or frozen applications use the
operating system's user data directory. `RECLAIM_HOME` overrides the root;
the `archive` and `profile` keys in `.reclaim.json` can choose other paths.
The profile contains browser sessions and is never part of a release asset.

The desktop package is made with `flet pack` and PyInstaller. The native
provider imports in `core/service.py` are explicit so PyInstaller can include
them. Chrome or Chromium comes from the user's system, or from an existing
Playwright browser installation; it is not bundled in the desktop download.
Flet's `flet test` uses a different Flutter host, so a separate packaged-app
smoke test is part of release verification.

The CLI additionally supports offline Google AI Studio folder parsing,
ChatGPT and Kept imports, and HAEVN Markdown export. These modes use the
same writer and database format without launching a browser.
