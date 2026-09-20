# CLI contract

The CLI remains available independently of the Flet GUI. Install the base
package for CLI use; the `gui` extra is optional.

```text
reclaim <provider> [TITLE] [options]   update one native provider
reclaim all [options]                  update enabled native providers
reclaim status [-o DIR]                offline archive status
reclaim gui                            launch the Flet GUI
reclaim --version                      version derived from Git tags
```

The six native provider names are `googleaistudio`, `deepseek`, `kimi`,
`chatgpt`, `claude`, and `googlegemini`. Common options are `--rebuild`,
`--dry-run`, `--list`, `--log`, `--skip N`, `--limit N`, `--no-raw`, and
`-o/--output-dir`. A `TITLE` match or `--url URL` fetches only matching chats
freshly. Default mode fetches new and changed chats according to the local
sync record. `--dry-run` contacts the provider, classifies its current list,
and prints the proposed work without writing archive files.

`reclaim all` reads `providers` from `.reclaim.json`, skips disabled
providers, and uses one shared browser session. An explicit single-provider
command ignores the enabled list. The GUI uses the same controller for
selected, all, dry-run, and rebuild operations.

Offline modes remain available:

```text
reclaim parse googleaistudio --from-folder DIR
reclaim import chatgpt CONVERSATIONS.json
reclaim import scrapemychats EXPORT_DIR
reclaim import kept VAULT [--providers kimi,claude]
reclaim export haevn-md . archive.zip
```

Default terminal output shows the run header, summary, and failures. `--log`
adds per-chat lines and timing. The GUI receives structured progress events
from the same provider loops; it does not parse terminal output.
