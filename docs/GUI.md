# Desktop GUI

The Flet window manages database updates for all six native providers in a
provider table with per-row status chips, saved chat counts, **last download**
and **last checked** times, and **Log in** / **Review** buttons. On startup
without settings or an initialized database, a **First-run setup** banner
guides folder selection, Chrome detection, provider selection, and logging in.
When any provider waits for manual sign-in, a banner shows which provider needs
a login with a **Bring browser forward** button. Startup reads local files
only and checks for newer GitHub releases non-blockingly. It shows live remote
changes only after **Check for updates**, **Review**, or an update contacts a
provider.

Use **Update selected** for checked providers or **Update all** for every
provider enabled in settings, and **Stop** to end an active run cleanly after
the current chat finishes saving. **Check for updates** lists current provider
chats and previews changes without writing to the database. **Download
everything again** asks for confirmation, then fetches all selected chats
again. Provider rows show both **last download** and **last checked** times,
and the header shows the installed version and a banner when a newer GitHub
release is available. The progress bar and Details panel show the operation;
provider errors appear in the summary and details. Playwright work runs in a
worker thread, keeping the window responsive while login or downloads take time.

Problems that would make a run unsafe appear in a banner under the title,
and the update buttons stay disabled until they are solved:

- **Invalid settings file:** the banner explains what is wrong, with
  *Open settings folder* and *Reset settings* (which keeps the old file as
  `.reclaim.json.invalid-<time>`).
- **Database folder missing or unusable:** *Retry* (after connecting a
  drive or synced folder), *Choose folder*, and *Create database here*,
  which asks for confirmation first.
- **Another run in progress:** a dialog names the computer and process
  holding the lock. When that run cannot be running here, *Remove lock*
  clears it.

Warnings during a run, such as a `raw.json` that could not be written, are
counted in the summary and listed in Details. Completed updates also send a
desktop notification with the run summary. **Open log folder** shows the run log,
which keeps the full output of every run, including errors. **Tidy duplicates**
scans for multiple folders belonging to the same chat, previews them, and retires
safe duplicates while preserving copies that contain unique branch text
(`reclaim tidy [--apply]`). **Archived & deleted** opens a review dialog for
chats in `_Archived` and `_Deleted` to open their folders, restore them to
active folders, or permanently delete them after confirmation. When **Check for
updates** finds new or changed chats, a review dialog lets you merge all, merge
a checked subset, or skip.

**Diagnostics** lists the state of this computer: version, settings,
database (location, chat count, write access, free space), the database and
login data locks, the browser, the login data folder (readable by other
users, or inside a synced folder such as Dropbox or Syncthing) and the log
file, each with advice when something is wrong. *Run live check* logs in to
each enabled provider and lists its chats, writing nothing; each provider's
row then shows "Live check: logged in · N chats listed" or the error.
*Copy diagnostics* copies the results as text for a bug report. They contain
folder paths, which can include your user name, but never chat content.
`reclaim doctor [--live]` prints the same report. **Capture a chat** lets
you choose a provider, enter a chat URL, and click *Capture* to record that
chat's sanitized API responses (saved even if fetching fails), then shows
the saved path with *Open folder*. Open and check the file before sharing it.
`reclaim capture PROVIDER URL [-o FILE]` runs the same capture from the CLI.

**Settings** writes the shared `.reclaim.json` (version 2, migrating older
settings automatically). It can choose enabled providers, a database folder, an
optional login data folder, the Kimi region (`international` vs `mainland`),
an optional Chrome or Chromium executable path, the login wait in seconds,
whether to check for updates at startup without waiting for logins, and whether
to start the app when logging in to the computer. Choosing an empty or new database folder creates the database there after a
confirmation; a folder with unrelated files is refused. The login data folder
field is useful when a standalone download should reuse a checkout's
`.playwright-profile`; point it at that folder. The field stores a path, not
account passwords. Only one run uses a profile at a time: a second run, from
the window or the command line, is refused and told which process holds it.
A newly installed desktop download otherwise creates its own profile in the
operating system data folder described in the README.

## Run from source

```bash
python -m pip install -e '.[gui]'
reclaim gui
# equivalent direct entry point:
reclaim-gui
```

`reclaim gui` reports how to install the extra if Flet is absent. On Linux,
the directory chooser uses `zenity`; the path can always be typed directly.
Chrome or Chromium must be available on the machine or in Playwright's
browser cache. The desktop package does not include Chromium.

## Test and package

```bash
python -m pip install -e '.[gui-test]' build ruff pyright
python -m unittest discover -s tests -q
ruff check reclaim tests scripts flet_integration
pyright reclaim tests scripts flet_integration
flet test linux flet_integration --tests-dir tests --yes
python scripts/pack_desktop.py --version 0.0.0
```

The Flet device tests run the real controls with a deterministic fake
controller. Linux device tests require Flutter, a display, and the packages
listed in [Flet's Linux build
requirements](https://flet.dev/docs/publish/linux/). The separate `flet pack`
build uses PyInstaller. `scripts/smoke_desktop.py PACKAGE` extracts and launches
the actual packaged application in an isolated directory, checks its GUI and
Playwright driver, and closes it. CI uses this on Linux, Windows, and macOS.
These startup checks need no provider accounts. A real authenticated dry run
and update still need manual testing with your own account.

Flet is pinned to 1.0.5. Version 1.0.4 fixed the Flutter JNI dependency
conflict that broke fresh integration-test builds with 1.0.0.

The Linux package also contains a `.desktop` template. If you install it in
your application menu, set its `Exec=` line to the absolute path of the
extracted `ReclaimMyChats` executable first.
