# Roadmap

Planned work for Reclaim My Chats, in the order it will be done. The phases
come from the November 2026 engineering assessment of stability,
maintainability and ease of use. Finished tasks are ticked here and
described in the document they affect.

## Principles

1. **The window is the main interface.** Everything a user needs works in
   the desktop app without a terminal: setup, logins, checking and saving
   chats, reviewing changes, settings and troubleshooting. The command line
   offers the same operations for scripts and experienced users. A feature
   is finished only when it works in the window; a feature that exists
   only on the command line is not done.
2. **Never lose data, never delete on the user's behalf.** Updates add
   chats, replace them with newer copies or move folders. Permanent
   deletion is always an explicit user action.
3. **Fail loudly and safely.** When the settings are invalid, the database
   folder is missing, another run is in progress or a provider changed,
   stop and say what happened and how to fix it, in the window and on the
   command line. Never fall back silently.
4. **Check against the real services before every release.** Synthetic
   tests cannot see provider changes. A read-only live check of every
   provider is part of each release.

Terms: the **database** is the local folder holding every saved chat.
**Archived** means only a provider's own archive feature. **Deleted** means
a chat no longer exists at the provider.

Each task below names what the window offers (**Window**) and what the
command line offers (**CLI**) where the task is visible to users.

## Phase 0. Release the provider fixes

- [ ] Merge `develop` into `main`. This publishes the fixes for ChatGPT,
      Claude, DeepSeek, Kimi and the Google logins to users of the
      desktop download.

## Phase 1. Protect the database

Goal: no interruption, crash, settings mistake or missing folder can lose
progress records or silently start a new, empty database.

- [x] Save sync records after every chat, and write every file atomically
      (a temporary file, then a rename). An interrupted run keeps the
      records of every chat it saved, which also makes a Stop button safe.
- [x] Validate the settings file at start. An invalid file stops with a
      clear message instead of falling back to defaults. **Window:** a
      banner with *Open settings folder* and *Reset settings*. **CLI:** the
      message and exit code 2.
- [x] Mark each database with `.reclaim-database.json`, which records its
      format version. A missing database folder is never created on the
      fly, except the default location on first use. Existing databases
      are recognised and marked automatically. **Window:** a banner with
      *Retry*, *Choose folder* and *Create database here*; choosing an
      empty folder in Settings creates the database. **CLI:** `reclaim
      init`.
- [x] Lock files in the database and the login data folder, naming the
      computer and process. A lock left by a process that no longer runs
      on this computer is cleared automatically. **Window:** a dialog that
      names the holder and offers *Remove lock*. **CLI:** `reclaim
      unlock`.
- [x] Run every command through the shared controller, so these checks
      apply however a run starts. (Moved forward from Phase 2.)
- [x] A rotating log file in the data folder that records every run,
      including provider output and errors. Failed `raw.json` writes are
      reported. **Window:** *Open log folder*.
- [x] Safe, unique folder and file names: no trailing dots or spaces and
      no names Windows reserves (such as CON or AUX). Two different chats
      with the same title get separate folders instead of sharing one.
- [x] `chat.json` records the provider's timestamp, the save time, the
      chat's state and a format version. Missing or damaged sync records
      are rebuilt from the chat files, keeping the damaged file as a
      backup. Chats saved earlier get their timestamps when the database
      is first marked.

## Phase 2. One provider engine

Goal: a fix made once reaches every provider, and provider changes are
caught before users see them.

- [x] A shared engine for the download loop, filters, dry runs, saving
      and the run record (`core/engine.py`), and one request policy for
      retries and rate limits that honours `Retry-After`
      (`core/retry.py`). Each provider supplies only how to log in, list
      its chats, read a chat URL and fetch one chat. The shared login
      check is part of Phase 3's login task.
- [x] Progress reported only as structured events, rendered once each for
      the window, the terminal and the log file (`core/events.py`,
      `core/render.py`). The window now shows when a provider waits for a
      login.
- [ ] A capture tool that records a real provider response with every
      text value replaced, so a format change becomes a test fixture.
- [x] Diagnostics: offline checks (browser, settings, database, locks) and
      a read-only live check that lists every provider. **Window:** a
      Diagnostics panel with *Copy diagnostics*. **CLI:** `reclaim doctor`
      and `reclaim doctor --live`.
- [x] Release checklist: the live check runs before merging into `main`
      ([releasing](RELEASING.md)).
- [ ] CI on Python 3.12 and 3.14, a lock file for builds, ruff's bugbear
      and pyupgrade rules, and a weekly workflow that reports new commits
      in the reference projects listed in the
      [provider review](UPSTREAM_REVIEW.md).

## Phase 3. A clearer app

Goal: someone who never opens a terminal can set up the app, log in,
update the database and solve common problems.

- [ ] First-run setup: find an existing database and login data folder or
      create new ones, choose providers, check that Chrome is installed,
      and log in to each provider.
- [ ] Main window as a provider table: a status chip, chats in the
      database, last checked, and a *Log in* or *Review* button per row.
- [ ] One shared login check for every provider (a real request, not
      only a cookie or token), run by the engine, and the same login wait
      everywhere. A visible banner says which provider needs a login, with
      a button that brings the browser window forward.
- [ ] A *Stop* button that ends a run after the current chat.
- [ ] Plain labels: *Check for updates*, *Download everything again*,
      *Login data folder*. Show "last checked" separately from "last
      download".
- [ ] Every setting in the window, including the Kimi region, the Chrome
      location and the login wait. Environment variables remain only as
      overrides.
- [ ] Version number and a notice when a new release is available.
- [ ] A version number in the settings file, with migrations.

## Phase 4. Follow chats at the provider

### Goal

An update mirrors what happened in the chat app since the last run: new
and edited chats, renamed chats, chats moved between projects or
notebooks, archived chats and deleted chats. The database remains a
backup: chats that are archived or deleted at the provider move into
separate folders, where the user decides whether to keep them.

Updates follow a *check, review, merge* flow. *Check* contacts the
providers and builds a plan without writing anything. *Review* shows the
plan per provider; the user merges everything, merges selected items or
skips. *Merge* downloads and moves only the approved items.

### Folder layout

```
<database>/<Provider>/
├── <Project>/<Chat>/            # active chats, as today
├── <Chat>/
├── _Archived/<Project>/<Chat>/  # archived at the provider
└── _Deleted/<Project>/<Chat>/   # no longer at the provider
```

The relative path inside `_Archived` and `_Deleted` is the chat's last
active path, so a chat can be restored by moving it back. The leading
underscore keeps these folders apart from chat and project names. The
writer must rename a project or chat whose folder name would equal one of
them.

### Lifecycle events

| At the provider | Detected by | Database action |
|:--|:--|:--|
| New chat | Listed, no sync record | Download (today) |
| Edited chat | Listed, newer timestamp | Download again (today) |
| Renamed, or moved to another project | Listed title or project differs from the record | Download again and move the folder (done when the timestamp also changes) |
| Archived | In the provider's archived listing | Move to `_Archived/` |
| Unarchived or restored | Listed again with an `_Archived`/`_Deleted` record | Move back, then update normally |
| Deleted | Missing from a complete listing and confirmed missing | Move to `_Deleted/` |

When a chat is saved in a new folder, its previous folder is already
retired (`retire_previous`). Still missing: the sync record does not store
the title or project, so a rename that leaves the provider timestamp
unchanged is skipped. Store both, and treat a difference as a change.

### Safety rules

These rules exist because an API change can make every chat look deleted;
in November 2026 a DeepSeek and a Kimi listing change each made complete
listings look broken.

1. **Never delete.** Updates only move folders within the provider folder.
2. **Only complete listings count.** Absence is evaluated only after a
   provider listing finished every page without errors, rate limits or
   truncation. Otherwise no lifecycle move happens and the run says why.
3. **Confirm each absence.** Before moving a chat to `_Deleted`, fetch it
   directly: "not found" confirms deletion, an archived flag or archived
   listing means `_Archived`, and any other result leaves it in place as
   "unconfirmed".
4. **Mass-change brake.** If a run would move more than a small share of a
   provider's chats (proposed: 10% or 20 chats, whichever is smaller), it
   moves nothing until the user confirms in the review.
5. **Audit trail.** Each move is appended to a per-provider event log
   (time, chat id, from, to, reason), and `chat.json` records the state
   (`active`, `archived`, `deleted`).

### Provider capabilities (verify live before enabling each)

| Provider | Archive feature | Deletion signal | Grouping |
|:--|:--|:--|:--|
| ChatGPT | Yes: `is_archived` on conversations; archived listing to confirm | Conversation request fails | Projects (supported) |
| Claude | None known for chats | HTTP 404 on the conversation | Projects exist; not yet mapped to folders |
| Gemini | None known | Absent from listing; read request to confirm | Gems/notebooks: to investigate |
| AI Studio | None | Absent from `ListPrompts` (Drive file trashed) | None |
| DeepSeek | None | Absent from listing; history request to confirm | None (pinned only) |
| Kimi | None known | Absent from listing; message request to confirm | Projects: listing uses `project_id: ""`; check whether project chats are included |

Start with ChatGPT, which has the clearest signals, then add the others
one at a time with synthetic tests of each response shape.

### Tasks

- [ ] Sync records with title, project and state.
- [ ] A completeness flag returned by every provider listing.
- [ ] A lifecycle planner shared by all providers: classify, confirm,
      apply the brake, report.
- [ ] ChatGPT archived listing and deletion confirmation, then the other
      providers one at a time, including Claude, Kimi and Gemini grouping.
- [ ] Moves into and out of `_Archived` and `_Deleted`, with the event
      log. (The writer already reserves both names.)
- [ ] Check without writing, and merge of an approved subset.
- [ ] Tidy: find chats saved in more than one folder and retire stale
      copies after a preview. Covers the duplicates created before
      November 2026. **Window:** a *Tidy* action with a preview.
      **CLI:** `reclaim tidy`.
- [ ] **Window:** the review dialog (per-provider lists with checkboxes,
      merge all, merge selected, skip); a review screen for `_Archived`
      and `_Deleted` to open, restore or permanently delete chats with
      confirmation; counts per state in the provider table.
      **CLI:** the same plan in `--dry-run`, and `reclaim status` counts
      per state.
- [ ] **Window:** setting to check for updates when the app starts (off
      by default). Opening the app then runs *Check* and shows *Review*
      when something was found. The automatic check never waits for a
      login; a provider that needs one shows *Log in*.
- [ ] **Window:** setting to start the app at login (off by default),
      creating a per-user autostart entry: an XDG autostart `.desktop`
      file on Linux, a `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`
      value on Windows, and a login item on macOS. Turning it off removes
      the entry.
- [ ] A run summary after each merge, also as a desktop notification.

## Phase 5. Packaging and updates

Goal: installing, updating and removing the app works the way people
expect on each system, and fixes reach users quickly.

- [ ] Build the desktop apps from the GUI dependencies only, removing
      about 20 MB of unused libraries.
- [ ] Linux: an AppImage with an unpacked folder inside (faster start) and
      a `.deb` for Debian, Ubuntu and Mint, both with a menu entry and
      icon. Build on an older base (glibc 2.35 or lower) so Ubuntu 22.04,
      Debian 12 and Mint 21 can run it; v3.3.3 needs glibc 2.38.
- [ ] Windows: a per-user installer that needs no administrator rights,
      with a Start menu entry, an uninstaller and the start-at-login
      option.
- [ ] macOS: a `.dmg` with drag-to-Applications, plus an Intel build.
- [ ] PyPI releases, so the command line installs with `pipx install
      reclaimmychats`.
- [ ] Build-provenance attestations on release files.
- [ ] Later, if users need it: code signing for Windows and macOS (paid).

Not planned: a Flatpak or Snap, whose sandbox blocks starting the
installed Chrome and writing to any folder the user chooses, and a
container, which cannot show a browser for logins on the user's desktop.
