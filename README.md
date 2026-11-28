# Reclaim My Chats

Reclaim My Chats saves conversations from ChatGPT, Claude, Google Gemini,
Google AI Studio, DeepSeek, and Kimi as local Markdown, JSON, and downloaded
media. It has a desktop synchronization manager and a full command line
interface (CLI). Your chats are saved in a local database: a folder on your
computer, or one you choose.

## Download the desktop application

Download the package for your operating system from [GitHub
Releases](https://github.com/matgir99/Reclaim-My-Chats/releases). Desktop
downloads begin with `v3.3.0`; the earlier `v3.2.0` release has no executable
assets. The application contains Python and Flet, so you do not need to
install either to run the download.

| System | Release asset | How to start |
|:--|:--|:--|
| Linux (AppImage) | `Reclaim-My-Chats-*-linux-x86_64.AppImage` | Mark executable (`chmod +x`) and open or run it |
| Linux (Debian/Ubuntu/Mint) | `Reclaim-My-Chats-*-linux-x86_64.deb` | Install with `sudo apt install ./Reclaim-My-Chats-*-linux-x86_64.deb` |
| Linux (portable tarball) | `Reclaim-My-Chats-*-linux-x86_64.tar.gz` | Extract it and run `ReclaimMyChats/ReclaimMyChats` |
| Windows (installer) | `Reclaim-My-Chats-*-windows-x86_64-setup.exe` | Run the per-user installer (no admin rights required) |
| Windows (portable zip) | `Reclaim-My-Chats-*-windows-x86_64.zip` | Extract it and open `ReclaimMyChats/ReclaimMyChats.exe` |
| macOS (Apple Silicon & Intel) | `Reclaim-My-Chats-*-macos-{arm64,x86_64}.dmg` (or `.zip`) | Open the `.dmg` and drag `ReclaimMyChats.app` to Applications |

Install Chrome or Chromium if the application reports that no browser is
available. The first update of each provider may show a browser window for
you to log in. Reclaim My Chats keeps that session in a local Playwright
profile and uses it for subsequent updates. The desktop download does not
contain a browser or account credentials. Unsigned Windows and macOS builds
may show their usual security prompts.

The main window provides a **First-run setup** wizard (database and login
folders, Chrome detection, enabled providers, and initial login) and a
provider table with status chips and per-row **Log in** / **Review** buttons.
It lets you select providers, update selected or all enabled providers, stop an
active run after the current chat, check for updates without writing to the
database, confirm changes in **Review updates**, manage **Archived & deleted**
chats, retire duplicates with **Tidy duplicates**, inspect **Diagnostics**, bring a
browser window awaiting login forward, inspect progress and errors, and open or
change the database folder. **Download everything again** fetches every selected
conversation again and overwrites its copy in the database. **Check for updates**
contacts the providers to list conversations, but does not write to the database.

See [GUI usage and testing](docs/GUI.md) for settings, profile migration,
and development commands.

## Install and use the CLI

The CLI requires Python 3.12 or newer and a Chrome or Chromium browser.
It is not published on PyPI. Download `reclaimmychats-VERSION-py3-none-any.whl`
from the [latest release](https://github.com/matgir99/Reclaim-My-Chats/releases/latest)
and install it with `pipx`, replacing `VERSION` with the number in the file name:

```bash
pipx install ./reclaimmychats-VERSION-py3-none-any.whl
reclaim --version
reclaim status
```

Or clone the repository and install from source:

```bash
git clone https://github.com/matgir99/Reclaim-My-Chats.git
cd Reclaim-My-Chats
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
reclaim --version
reclaim status
```

On Windows, create the environment with `py -m venv .venv` and activate it
with `.venv\Scripts\Activate.ps1`. If you want to launch the GUI from Python,
install the optional dependency: `python -m pip install -e '.[gui]'`, then run
`reclaim gui` or `reclaim-gui`.

Common commands:

```bash
reclaim chatgpt                       # update new and changed chats
reclaim claude --dry-run              # preview after contacting Claude
reclaim googlegemini --rebuild        # fetch every Gemini chat again
reclaim googleaistudio "latex"       # one title match
reclaim deepseek --list               # list titles without saving
reclaim kimi --url CHAT_URL           # one exact chat
reclaim all                           # update enabled providers in one browser session
reclaim status                        # inspect the local database offline
```

The other modes remain available:

```bash
reclaim parse googleaistudio --from-folder DIRECTORY
reclaim import chatgpt EXPORT.zip       # official export: ZIP, folder or conversations.json
reclaim import kept VAULT --providers kimi,claude
reclaim import scrapemychats EXPORT_DIRECTORY
reclaim export haevn-md . chats.zip
reclaim init [FOLDER]                 # create a database (default: the configured folder)
reclaim unlock                        # remove a lock left by a run that has ended
reclaim doctor                        # check settings, database, locks, browser, login data
reclaim doctor --live                 # also log in to each provider and list its chats
reclaim capture PROVIDER URL [-o FILE]  # record sanitized responses for one chat
reclaim tidy [--apply] [-o DIR]       # preview or retire duplicate chat folders
```

Run `reclaim <provider> --help` for `--skip`, `--limit`, `--log`, `--no-raw`,
`--only-id`, and `-o/--output-dir`. Single provider commands run even if that
provider is disabled in settings; `reclaim all` uses the configured provider
list.

See the [provider compatibility review](docs/UPSTREAM_REVIEW.md) for current
format support, upstream sources, and limits on older or very long histories, and
the [roadmap](docs/ROADMAP.md) for completed phases and remaining optional work.

## Database and settings

The **database** is the folder where Reclaim My Chats saves every chat it
downloads. **Archived** and **deleted** refer only to states at the provider:
when a provider archives or confirms deletion of a chat, Reclaim My Chats moves
its saved folder into `_Archived/` or `_Deleted/` inside that provider's folder
rather than deleting user files (recording moves in `.reclaim_events.jsonl` and
halting large batches behind a confirmation brake). Use **Archived & deleted** in
the window to inspect, restore, or explicitly remove them.

Each provider writes to `<database>/<Provider>/<chat>/`. A chat folder contains
readable Markdown, `chat.json`, optional `raw.json`, and downloaded media.
ChatGPT Projects become subfolders. A run manifest and sync record in each
provider folder support incremental updates and the offline status view.
See [the output contract](docs/OUTPUT.md) for details.

The app protects the database:

- A file named `.reclaim-database.json` marks the database folder. If the
  folder is missing, for example on a drive that is not connected, the app
  stops and says so instead of starting a new, empty database. Only the
  default location is created on first use. Create a database elsewhere
  with **Create database here** in the window or `reclaim init FOLDER`.
  Databases made by earlier versions are recognised and marked
  automatically.
- One run at a time: a lock file in the database and in the login data
  folder names the computer and process using them. A lock left by a crash
  on the same computer is cleared automatically; remove one left by another
  computer with **Remove lock** in the window or `reclaim unlock`.
- Progress is saved after every chat and every file is written atomically,
  so closing the window or a crash mid-run loses nothing already saved.
- An invalid settings file stops the app with an explanation instead of
  silently using the defaults. **Reset settings** in the window moves the
  file aside.
- Each run is logged to `logs/reclaim.log` in the data folder. In the window,
  **Open log folder** shows it.

When something does not work, **Diagnostics** in the window (or
`reclaim doctor`) checks the settings, the database, locks, the browser and
the login data folder, and says what to do about each problem. *Run live
check* (`reclaim doctor --live`) also logs in to each enabled provider and
lists its chats without saving anything. *Copy diagnostics* copies the
results for a bug report: they contain folder paths, never chat content.
When a provider fails on a conversation, *Capture a chat* in Diagnostics (or
`reclaim capture PROVIDER URL [-o FILE]`) records that chat's API
responses with the chat's text replaced while keeping the structure, field
names, numbers, dates and parser words; it saves the capture even if the
fetch fails. Only direct API/RPC responses are recorded (not Google AI
Studio's page-interception or DOM fallbacks), and login/token requests stay
outside the recorder. Open and read the saved file before sharing it.

The GUI and CLI use the same `.reclaim.json` settings. For a source checkout,
the default database is `<checkout>/chats` and the default browser profile is
`<checkout>/.playwright-profile`. Installed Python packages and standalone
desktop downloads use an operating system data folder instead:

| System | Default data folder |
|:--|:--|
| Linux | `$XDG_DATA_HOME/ReclaimMyChats` or `~/.local/share/ReclaimMyChats` |
| macOS | `~/Library/Application Support/ReclaimMyChats` |
| Windows | `%LOCALAPPDATA%\ReclaimMyChats` |

The database is `chats/` and the browser profile is `.playwright-profile/`
inside that data folder by default. Set `RECLAIM_HOME` to use an existing
checkout or another data folder. In the GUI, **Settings** configures enabled providers, the database folder, an
optional login data folder (for reusing login sessions from a checkout without
copying cookies), the Kimi region (`international` or `mainland`), an optional
Chrome or Chromium executable path, the manual login wait (two minutes by default), whether to
check for updates on startup without blocking on logins, whether to start
the app at system login, and whether an update replaces older copies of a chat
automatically (off by default; an older copy is removed only when everything it
says is in the current copy). Desktop notifications are sent when merges
complete unless `RECLAIM_NO_NOTIFY=1` is set. Environment variables
(`RECLAIM_KIMI_URL`, `RECLAIM_CHROME_PATH`, `RECLAIM_LOGIN_TIMEOUT`)
remain available as overrides. Do not open two Reclaim processes with the same
profile simultaneously.

For manual configuration (settings version 2; the `archive` key names the
database folder for compatibility):

```json
{
  "version": 2,
  "providers": ["chatgpt", "claude"],
  "archive": "/path/to/chats",
  "profile": "/path/to/existing/.playwright-profile",
  "kimi_region": "international",
  "chrome_path": "/usr/bin/google-chrome",
  "login_timeout": 120,
  "check_on_startup": false,
  "start_at_login": false,
  "retire_older_copies": false
}
```

Relative paths are resolved from the data folder. `"archive": "."` puts
provider directories directly in that folder. `.reclaim.json`, the database,
and the profile are ignored by Git when they live in a checkout. The profile
holds live login sessions; treat it like a browser profile. No account
passwords are stored in the settings file.

## Develop Reclaim My Chats

`develop` is the integration branch. `main` contains released code and remains
the default branch. All changes reach `main` through a tested pull request from this repository's
`develop` branch. Contributor and Dependabot PRs must target `develop` first;
retarget any Dependabot security PRs opened against `main` to `develop`. Merging triggers the release workflow: it builds and verifies
the Python and desktop packages, then creates the version tag and publishes
the GitHub Release. The first GUI release is `v3.3.0`; ordinary later merges
increment the patch number. No version edit or special commit message is
needed. See [releasing](docs/RELEASING.md) and the short
[maintainer guide](docs/MAINTAINING.md).

Report reproducible bugs through [Issues](https://github.com/matgir99/Reclaim-My-Chats/issues/new/choose)
and ask usage questions in [Discussions](https://github.com/matgir99/Reclaim-My-Chats/discussions).
Exclude real conversations, credentials, and browser profiles from reports.
Use [private reporting](SECURITY.md) for security problems. This is a hobby
project with no guaranteed response time.

```bash
uv sync --extra gui-test      # the versions pinned in uv.lock, plus build, ruff, pyright
# or: python -m pip install -e '.[gui-test]' build ruff pyright   (newest versions)
python -m unittest discover -s tests -q
python -m playwright install --only-shell chromium
python -m unittest discover -s tests -p browser_protocols.py
ruff check reclaim tests scripts flet_integration
pyright reclaim tests scripts flet_integration
python -m build
flet test linux flet_integration --tests-dir tests --yes
python scripts/pack_desktop.py --version 0.0.0
```

`uv.lock` pins every dependency that CI and the release builds install.
After changing dependencies in `pyproject.toml`, run `uv lock` and commit
both files; Dependabot keeps the lock up to date monthly.

The Flet device tests require the Flutter Linux build prerequisites listed in
[Flet's Linux guide](https://flet.dev/docs/publish/linux/). `flet pack` uses
PyInstaller and does not need that Flutter toolchain. A packaged executable
must also be launched outside the development environment as a separate
smoke test.

The six native provider modules share browser, manifest, and writer code in
`reclaim/core`. The CLI and Flet GUI use the same `ArchiveController`; the GUI
runs its blocking provider work in a worker thread. See [architecture](docs/ARCHITECTURE.md).

## License

MIT. See [LICENSE](LICENSE) and [NOTICE.md](NOTICE.md).
