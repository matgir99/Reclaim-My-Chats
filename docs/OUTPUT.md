# Database output

The database is the folder holding every saved chat. Its default location is
`chats/` inside a checkout or the application's data folder. Set `archive` in
`.reclaim.json` (the key name is kept for compatibility) or use `-o` on a
provider command to choose another destination. Provider folders have these
names:

| Provider command | Folder |
|:--|:--|
| `googleaistudio` | `Google AI Studio` |
| `deepseek` | `Deepseek Chat` |
| `kimi` | `Kimi Chat` |
| `chatgpt` | `ChatGPT` |
| `claude` | `Claude` |
| `googlegemini` | `Google Gemini` |

```text
chats/
├── .reclaim-database.json         marks the folder as a database
├── ChatGPT/
│   ├── .last_sync_chatgpt.json    sync records
│   ├── .reclaim_manifest.json     last run
│   └── Example project/
│       ├── Example conversation/
│       │   ├── Example conversation.md
│       │   ├── chat.json
│       │   ├── raw.json
│       │   ├── image_1.png
│       │   └── notes.pdf
│       └── Example conversation (2)/   another chat with the same title
└── Claude/
    └── ...
```

## The database folder

`.reclaim-database.json` records the database format and when it was
created. The app uses it to tell a database from an empty or unrelated
folder. A missing database folder, for example on a drive that is not
connected, is reported instead of replaced by a new, empty one. Only the
default location is created automatically on first use; any other folder
is created with `reclaim init` or with *Create database here* in the window.
A database made before the marker existed is recognised by its provider
folders and marked on the first update; the provider timestamps from its
sync records are then copied into each `chat.json`.

While an update runs, `.reclaim.lock` in the database names the computer and
process using it, and a second run is refused. The same applies to the
login data folder. A lock left by a process that has ended on the same
computer is cleared automatically; `reclaim unlock` or *Remove lock* in the
window removes one left by another computer.

## Chat folders

A chat folder contains its Markdown, `chat.json`, an optional
media-stripped `raw.json`, and the chat's images and attachments. ChatGPT
Projects add a project folder. A folder belongs to exactly one chat (the id
in its `chat.json`): another chat with the same title gets `Title (2)`,
`Title (3)` and so on. Names are valid on Windows, macOS and Linux: no
trailing dots or spaces, and names Windows reserves, such as `CON`, get an
underscore. Files with the same name within one chat get `_1`, `_2`
suffixes, compared without case; an attachment can never replace the chat's
own Markdown, `chat.json` or `raw.json`.

The Markdown contains turns in order, preserving LaTeX text and local links
to saved media. Provider thought content is omitted where the provider
identifies it; a structural marker can indicate that omission. `raw.json`
retains a scrubbed provider response to help recover from parser errors.

`chat.json` contains:

| Field | Meaning |
|:--|:--|
| `format` | Format version of this file (1) |
| `id`, `title`, `source_url`, `provider`, `project` | The chat and where it came from |
| `provider_updated_at` | The provider's own timestamp for this version of the chat |
| `saved_at` | When this copy was written (UTC) |
| `state` | `active`; `archived` and `deleted` are planned (see the roadmap) |
| `markdown` | The file name of the chat's Markdown |
| `had_thoughts`, `errors` | Whether reasoning was omitted; provider-side errors |
| `turns` | Normalized turns with their saved images and attachments |

Files written before November 2026 lack `format`, `provider_updated_at`,
`saved_at`, `state` and `markdown` until the database is marked or the chat
is saved again.

## Records

Each provider's `.last_sync_<provider>.json` maps chat IDs to provider
timestamps and saved Markdown paths, relative to the provider folder.
Default update mode skips entries classified as unchanged. The record is
saved after every chat, so an interrupted run keeps the records of the
chats it saved. If the file is missing or damaged, it is rebuilt from the
`chat.json` files; a damaged file is kept beside it as
`.last_sync_<provider>.json.damaged-<time>`.

`.reclaim_manifest.json` records when the last run started, its duration,
per-chat results, and totals such as chats, successes, failures, images,
and documents. The window and `reclaim status` read these local records
and files; they do not infer live provider state without contacting it.

Runs also write `.reclaim_last_attempt.json` with the operation's start
time, exit code, and error. An operation failure is displayed separately
from chat failures, preserving the previous run manifest and its actual
chat counts. A later successful run clears or supersedes that failure.
Dry runs write neither sync records nor attempt records.

Every file in the database is written to a temporary file and then renamed,
so an interruption leaves either the old file or the new one.

## Outside the database

Each run copies what it prints, including warnings and error details, to
`logs/reclaim.log` in the data folder. The log rotates at 1 MB and keeps five
older files. It contains chat titles and error details, never chat text or
login data.
