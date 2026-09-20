# Architecture

Reclaim My Chats has two frontends over one set of native provider modules.
Both write the same on-disk archive format.

```text
reclaim CLI ───────┐
                   ├── ArchiveController ── shared Playwright session
Flet desktop GUI ──┘                        ├── ChatGPT, Claude, Gemini
                                          ├── Google AI Studio, DeepSeek, Kimi
                                          └── writer, sync state, run manifests

Offline import and export commands ─────── importers/exporters ── archive
```

`reclaim/core/service.py` parses provider arguments and runs selected native
providers in one browser session. The CLI's `reclaim all` and the GUI call
that controller. Individual provider CLI commands continue to call their
provider directly. The controller allows only one operation at a time, so
concurrent GUI clicks cannot open the same browser profile twice.

Provider modules acquire conversations from each service, normalize them to
the models in `reclaim/core/model.py`, and use `reclaim/core/writer.py` for
Markdown, JSON, and media output. Their sync records classify chats as new,
changed, or unchanged. `reclaim/core/manifest.py` records each run.

Providers emit `ProgressEvent` callbacks for run, provider, and chat
transitions. The Flet GUI receives these from a worker thread through an
asyncio queue, then updates controls on its own event loop. Archive status is
read from local manifests and files on another worker thread. Startup never
guesses whether a remote chat changed; dry run or update must contact the
provider to know that.

Settings, archive, and browser profile paths share one writable root. A Git
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
same archive writer or archive format without launching a browser.
