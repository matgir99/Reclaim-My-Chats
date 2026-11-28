# CLI contract

The CLI remains available independently of the Flet GUI. Install the base
package for CLI use; the `gui` extra is optional.

```text
reclaim <provider> [TITLE] [options]     update one native provider
reclaim all [options]                    update enabled native providers
reclaim status [-o DIR]                  offline database overview
reclaim init [DIR]                       create a database (default: the configured folder)
reclaim doctor [--live]                  check settings, database, locks, browser, logins
reclaim capture <provider> URL [-o FILE] record one chat's sanitized responses
reclaim tidy [--apply] [-o DIR]          preview or retire duplicate chat folders
reclaim unlock                           remove a lock left by a run that has ended
reclaim gui                              launch the desktop application
reclaim --version                        version derived from Git tags
```

The seven provider names are `googleaistudio`, `deepseek`, `kimi`,
`chatgpt`, `claude`, `googlegemini`, and `antigravity`. The last reads
Antigravity's conversations from this computer and never opens a browser;
its `--url` takes a conversation id (or the path of its `.db` file). Common
options are `--rebuild`, `--dry-run`, `--list`, `--log`, `--skip N`,
`--limit N`, `--no-raw`,
`--only-id ID` (repeatable to merge selected chats), and
`-o/--output-dir`. A `TITLE` match or `--url URL` fetches only matching chats
freshly. Default mode fetches new and changed chats according to the local
sync record. `--dry-run` contacts the provider, classifies its current list,
and prints the proposed work without writing database files.

`reclaim all` reads `providers` from `.reclaim.json`, skips disabled
providers, and uses one shared browser session. An explicit single-provider
command ignores the enabled list. If a provider cannot open or the login
times out, the other providers continue. The command reports the failing
provider and exits with code 2. The GUI uses the same controller (`ArchiveController`)
for selected, all, dry-run, rebuild, live check, and capture operations.

Database setup, locks, diagnostics, and captures also share controller/core logic:
`reclaim init [DIR]` creates or marks a database folder, `reclaim unlock` clears
stale locks left by another computer, `reclaim doctor [--live]` runs offline
health checks (and optional live provider checks), and `reclaim capture` saves
one chat's sanitized provider responses to `captures/<provider>-<time>.json`
(or `-o FILE`).

Offline modes remain available:

```text
reclaim parse googleaistudio --from-folder DIR [--titles MAP.json] [-o DIR]
reclaim import chatgpt EXPORT.zip [-o DIR]          # ZIP, folder or conversations.json
reclaim import scrapemychats EXPORT_DIR [-o DIR]
reclaim import kept VAULT [--providers kimi,claude] [-o DIR]
reclaim export haevn-md . chats.zip
```

These offline commands have no window screen, by decision (20 November 2026):
the window covers saving and following chats from the providers, and nobody
has needed an import or export there yet. If one is built, an import screen for
an official ChatGPT export comes first.

Default terminal output shows the run header, summary, and failures. `--log`
adds per-chat lines and timing. The GUI receives structured progress events
from the same provider loops; it does not parse terminal output.
