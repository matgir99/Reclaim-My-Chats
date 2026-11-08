# Roadmap

Planned work, in priority order. Each item lists what is decided and what
still needs a decision or live evidence. Completed items move to
[the provider review](UPSTREAM_REVIEW.md) or the relevant document.

## 1. Follow chats as they change at the provider

### Goal

An update should mirror what happened in the chat app since the last run:
new and edited chats, renamed chats, chats moved between projects or
notebooks, archived chats and deleted chats. The archive remains a backup:
an update never deletes a chat. Chats that disappear or are archived at
the provider are moved into separate folders, where the owner decides
whether to keep or delete them.

### Folder layout

```
<archive>/<Provider>/
├── <Project>/<Chat>/          # active chats, as today
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

| At the provider | Detected by | Archive action |
|:--|:--|:--|
| New chat | Listed, no sync record | Download (today) |
| Edited chat | Listed, newer timestamp | Download again (today) |
| Renamed, or moved to another project | Listed title or project differs from the record | Download again and move the folder (done for title and project changes that also change the timestamp; see below) |
| Archived | In the provider's archived listing | Move to `_Archived/` |
| Unarchived or restored | Listed again with an `_Archived`/`_Deleted` record | Move back, then update normally |
| Deleted | Missing from a complete listing and confirmed missing | Move to `_Deleted/` |

Already done: when a chat is saved in a new folder, its previous folder is
retired (`retire_previous`), and sync records store paths relative to the
provider folder. Still missing: the sync record does not store the title
or project, so a rename that leaves the provider timestamp unchanged is
skipped. Store both, and treat a difference as a change.

### Safety rules

These rules exist because an API change can make every chat look deleted;
in November 2026 a DeepSeek and a Kimi listing change each made complete
listings look broken.

1. **Never delete.** Updates only move folders within the provider folder.
   Permanent deletion is a separate, explicit owner action.
2. **Only complete listings count.** Absence is evaluated only after a
   provider listing finished every page without errors, rate limits or
   truncation. Otherwise no lifecycle move happens and the run says why.
3. **Confirm each absence.** Before moving a chat to `_Deleted`, fetch it
   directly: "not found" confirms deletion, an archived flag or archived
   listing means `_Archived`, and any other result leaves it in place as
   "unconfirmed".
4. **Mass-change brake.** If a run would move more than a small share of a
   provider's chats (proposed: 10% or 20 chats, whichever is smaller), it
   moves nothing and asks for confirmation (`--apply-lifecycle` in the CLI,
   a dialog in the GUI).
5. **Preview.** `--dry-run` lists planned lifecycle moves with the reason.
6. **Audit trail.** Each move is appended to a per-provider event log
   (time, chat id, from, to, reason), and the sync record keeps the status
   (`active`, `archived`, `deleted`) and the original path.

### Provider capabilities (verify live before enabling each)

| Provider | Archive | Deletion signal | Grouping |
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

- [ ] Sync record v2: title, project, status, original path; migrate v1
      records in place.
- [ ] Listing completeness flag returned by every provider listing.
- [ ] Lifecycle planner shared by all providers: classify, confirm, apply
      the brake, report.
- [ ] ChatGPT archived listing and deletion confirmation.
- [ ] Moves into and out of `_Archived` / `_Deleted`, with the event log.
- [ ] Reserve `_Archived` and `_Deleted` in the writer.
- [ ] `reclaim status` and the GUI: counts per status.
- [ ] `reclaim tidy`: find chat ids saved in more than one folder and
      retire stale copies with the same rules as `retire_previous`
      (preview first). Covers duplicates created before November 2026.
- [ ] Other providers, one at a time; Claude, Kimi and Gemini grouping.

## 2. Updates from the desktop application

### Goal

Keep the archive current without a terminal, on Linux, Windows and macOS,
while the owner stays in control of what enters the archive.

### Design

- **Check, review, merge.** An update has two phases. *Check* contacts the
  providers and builds a plan: new, changed, renamed or moved, archived,
  deleted and restored chats. It writes nothing; the existing dry run is
  its basis. *Review* shows the plan per provider; the owner merges all,
  merges selected items or skips. *Merge* downloads and moves only the
  approved items. Skipping writes nothing; the next check rebuilds the plan.
- **Check when the app starts** (setting, off by default). Opening the app
  runs Check and shows Review when something was found.
- **Start the app at login** (setting, off by default). Together with the
  previous setting this gives an update prompt after each login. The
  entries are per user: an XDG autostart `.desktop` file on Linux, a
  `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` value or a Startup
  shortcut on Windows, and a login item or LaunchAgent on macOS. Turning
  the setting off removes the entry. "At boot" means at login: the
  browser profile and accounts belong to the user's session.
- **Manual use.** Without the startup check, the **Check for updates**
  button runs the same Check → Review → Merge. Other commands are buttons
  too: dry run, rebuild, status, tidy, open archive, settings, and the
  `_Archived`/`_Deleted` review (section 3).
- **No surprise login windows.** The automatic check never waits for a
  login; a provider that needs one shows "login required" with a
  **Log in** button.
- **Profile lock.** One Reclaim process per browser profile, so the app
  and a CLI run cannot use it at the same time.

### Tasks

- [ ] Check that returns a plan without writing (dry run plus lifecycle
      moves from section 1).
- [ ] Merge that applies an approved subset of the plan.
- [ ] Review dialog: per-provider lists with checkboxes, merge all,
      merge selected, skip.
- [ ] Settings: check when the app starts; start the app at login, with
      autostart entries for the three systems.
- [ ] No-login mode for the automatic check, with per-provider login
      buttons.
- [ ] Profile lock file shared by the app and the CLI.
- [ ] Run summary after each merge, also as a desktop notification.

## 3. Desktop application

The Flet desktop application already ships for Linux x86_64, Windows
x86_64 and macOS (see [GUI](GUI.md)). Planned improvements besides
section 2:

- [ ] Review screen for `_Archived` and `_Deleted`: open, restore, or
      delete permanently with confirmation.
- [ ] Installers: AppImage or `.deb`, an MSI or setup program, and a
      `.dmg`; the Linux installer also adds a menu entry.
- [ ] Code signing for Windows and macOS notarization, removing the
      security prompts unsigned builds show.
- [ ] In-app update check against GitHub Releases.
