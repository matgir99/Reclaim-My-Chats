# Reclaim My Chats

Reclaim your AI chat history. Bulk-export conversations from AI chat
platforms into clean local Markdown folders — full text with LaTeX intact,
model thoughts filtered out, original-quality images, and real downloaded
attachments.

Supports **Google AI Studio**, **DeepSeek Chat**, **Kimi**, and **ChatGPT**
(incl. Projects). MIT licensed.

## Features

- **Native scrapers** over each platform's own internal data layer — no
  visual/DOM scraping, no manual copy-paste
- **ChatGPT Projects** mirrored as subfolders (`ChatGPT/<Project>/<title>/`)
- **Clean output**: structural thought flags (no model reasoning text),
  LaTeX `$...$`/`$$...$$` intact, original images, downloaded attachments
- **Incremental sync**: update by default — chats whose server timestamp
  matches the local sync record are skipped automatically; `--rebuild`
  re-fetches everything
- **Per-chat canonical dump**: `<title>.md` + `chat.json` (machine-readable)
  + `raw.json` (media-stripped provider response) + media files
- **Interop**: importers for third-party captures, exporter to HAEVN
  Markdown for search
- **Privacy-aware**: session cookies live only in a local git-ignored
  browser profile; nothing is uploaded anywhere

## Requirements

- **Python 3.12+** (any newer installed version is used automatically;
  the project is developed on 3.14)
- **Playwright** (`playwright>=1.61`)
- A system **Chrome or Chromium** install (auto-detected) — or Playwright's
  bundled Chromium (`python -m playwright install chromium`)

Platform support: **Linux is first-class**. **macOS** works via the bundled
bash scripts (portable fallbacks for `setsid`/`pkill`/`sort`). **Windows**:
use WSL2 for full support; the `reclaim` package itself runs under native
Git Bash (`python -m reclaim`), but `run.sh` (detached runner) is not
supported there.

## Install

```bash
git clone https://github.com/matgir99/Reclaim-My-Chats.git
cd Reclaim-My-Chats
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
# optional: if you don't have system Chrome/Chromium
python -m playwright install chromium
```

This gives you the `reclaim` command (or run `python -m reclaim ...`).

### Verify your install

```bash
reclaim --version     # e.g. reclaim 3.0.0
reclaim status        # offline overview; shows "not archived yet" on a fresh setup
```

If `reclaim` is not found: the venv isn't active — run `source .venv/bin/activate`
first, or just use `python3.14 -m reclaim ...` from the repo (no install needed).

## How it works

One program, per-provider modes (`reclaim <provider>` updates by default):

| Provider | Command | How |
|---|---|---|
| Google AI Studio | `reclaim googleaistudio` | Replays the app's own `ResolveDriveResource` RPC (SAPISIDHASH auth) — text, structural thought flags, inline original images, Drive attachment downloads |
| Google AI Studio (offline) | `reclaim parse googleaistudio` | Drive folder download / Takeout zip — no browser needed |
| DeepSeek | `reclaim deepseek` | Reads the `deepseek-chat` IndexedDB directly — raw markdown, citations mapped, thinking skipped |
| ChatGPT | `reclaim chatgpt` | Native REST (`/backend-api/`): conversations, Projects, two-step signed-URL file downloads |
| ChatGPT | `reclaim import chatgpt` | Official `conversations.json` export, or scrapemychats export dirs (incl. media) |
| Kimi | `reclaim kimi` | Native REST (`/apiv2/`, bearer from localStorage) |
| Kimi, Claude, Grok, Gemini | `reclaim import kept` | Kept vault (`~/.kept/vault`) — install Kept, sync, import |

Output contract per chat: `<Provider>/<Project>/<title>/<title>.md` +
`chat.json` + `raw.json` + media. Thoughts omitted, filenames de-duplicated,
manifests track every run (see `docs/ARCHITECTURE.md`).

## Quickstart

```bash
# Google AI Studio (first run opens a window for Google login)
reclaim googleaistudio                 # update: new + changed chats only
reclaim googleaistudio --rebuild       # everything, freshly (overwrite)
reclaim googleaistudio "latex"         # chats whose title contains "latex"
reclaim googleaistudio --list          # print chat titles, no download

# DeepSeek / Kimi / ChatGPT (first run logs in via browser window)
reclaim deepseek --dry-run             # preview what WOULD be fetched
reclaim kimi --log                     # update with verbose progress + ETA
reclaim chatgpt                        # update (includes ChatGPT Projects)
reclaim all                            # update all four providers, in order
reclaim all --rebuild                  # rebuild everything

# Archive overview (fully offline, no browser)
reclaim status

# Detached runs with PID + log (Linux/macOS bash)
./run.sh googleaistudio --rebuild      # ./run.sh progress | stop
./run.sh all                           # all providers, sequentially

# AI Studio offline (no browser): download the "Google AI Studio" folder
# from drive.google.com (or a Takeout zip), then
reclaim parse googleaistudio --from-folder ~/Downloads/"Google AI Studio" \
    --titles titles.json                # optional drive_id -> title map

# ChatGPT via official export (no browser):
# chatgpt.com → Settings → Data Controls → Export, then
reclaim import chatgpt ~/Downloads/conversations.json

# Kimi / Claude / Grok / Gemini via Kept (see docs/providers/kimi.md)
reclaim import kept ~/.kept/vault --providers kimi

# Push the whole archive into HAEVN's search UI
reclaim export haevn-md . archive.zip
```

### CLI surface

```
reclaim <provider> [TITLE] [options]     update: new + changed chats only
reclaim <provider> --rebuild [options]   everything, freshly (overwrite)
reclaim <provider> "latex"               chats whose title contains "latex"
reclaim <provider> --url URL             one exact chat
reclaim <provider> --list [TITLE]        print chat titles, no download
reclaim <provider> --log [options]       verbose progress + timings
reclaim <provider> --dry-run [options]   preview what would be fetched
reclaim status [-o DIR]                  offline archive overview
reclaim all [options]                    update all four providers, in order
```

Providers: `googleaistudio`, `deepseek`, `kimi`, `chatgpt` (`all` = every
provider). Common options: `--skip N`, `--limit N`, `--dry-run`, `--no-raw`,
`-o/--output-dir`. Naming a `TITLE` (or `--url`) always fetches those chats
freshly; nothing else is touched. `--dry-run` logs in and lists, prints what
a run would fetch (`would fetch: N (M new, K changed) · would skip: J
unchanged`), and downloads nothing.

Output has exactly two levels — no in between:

- **Default: essential info + summary.** Real runs print the `N chats`
  header and the `Done: X ok, Y failed` summary (failures always print);
  `--dry-run` prints the `would fetch: ...` counts line plus the affected
  titles.
- **`--log`: full log.** Every chat gets a line
  (`[i/N] Title -> Nt, N,NNN chars` / `-> skip (unchanged)`), plus verbose
  progress (`[i/N] NN% · elapsed M:SS · ETA M:SS`) and per-chat detail
  (path/files/timings). `--dry-run --log` prints the same per-chat lines
  with `fetch (new|changed|fresh)` / `skip (unchanged)` before the counts.

Authentication: you log in **once per provider** in a real browser window
that appears during the first run; sessions are stored in
`.playwright-profile/` (a normal Chromium profile, git-ignored, owner-only
permissions). Update runs need no re-login unless sessions expire. To
remove stored credentials at any time: log out of the sites in the profile
browser and delete `.playwright-profile/`.

## Troubleshooting

- **`reclaim: command not found`** — venv not active (`source .venv/bin/activate`),
  or use `python3.14 -m reclaim ...` from the repo without installing.
- **Login window doesn't appear / session expired** — log in to the site in the
  profile browser and retry; the wait is 15 min by default, override with
  `RECLAIM_LOGIN_TIMEOUT=1800` (seconds).
- **Browser profile locked (`SingletonLock`)** — a previous run left Chrome
  running: `./run.sh stop`, or delete `.playwright-profile/Singleton*`.
- **No system Chrome found** — install Chromium for Playwright and its Linux
  deps: `python -m playwright install chromium && playwright install-deps`.
- **Why does `--dry-run` still open a browser?** — by design: it logs in and
  lists chats to compute the preview, but downloads and writes nothing.

## Security notes

- The repo and its git history contain **no chat data, no cookies, no
  tokens** — the archive and the browser profile are git-ignored by design.
  Never force-add them.
- The browser profile holds live session cookies for your accounts; treat
  it like any other browser profile. Anyone with read access to your
  machine could use them (same as your everyday browser).
- `raw.json` strips long strings (e.g. time-limited signed download URLs).
- Never commit `output/*` or `.playwright-profile/`; `.gitignore` already
  covers all of this.

## Development

```bash
./scripts/run_tests.sh  # offline unit tests (fixtures, no browser)
ruff check reclaim/ tests/
pyright reclaim/ tests/
```

```
reclaim/
├── __main__.py            # unified CLI: <provider> | all | status | import | export | parse
├── core/                  # browser, manifest, model, progress, status, writer
├── providers/             # googleaistudio, aistudio_files (parse mode), deepseek,
│                          # chatgpt, chatgpt_import, kept_vault, kimi
└── exporters/             # haevn_md
docs/                      # plan, architecture, research, provider notes
tests/                     # fixtures + offline unit tests
scripts/                   # shared shell helpers (newest-python picker)
```

## Docs

- `docs/STATUS.md` — current status + task tracker
- `docs/PLAN.md` — original build plan (complete)
- `docs/ARCHITECTURE.md` — hub-and-spoke design rationale
- `docs/research/` — survey + analysis of existing OSS export tools
- `docs/providers/kimi.md` — Kimi/Kept details
- `NOTICE.md` — attribution and licensing notes
- `LICENSE` — MIT
