# Output layout

All scraping/importing writes into provider folders at the repo root (or
`-o DIR` if given). These folders are **git-ignored by design** — the
archive is your local data, never part of the repository.

## Folder anatomy

```
Google AI Studio/
└── My Chat Title/
    ├── My Chat Title.md        # human-readable markdown
    ├── chat.json               # canonical machine-readable dump
    ├── raw.json                # media-stripped provider response (insurance)
    └── media/                  # inline images + downloaded attachments
                                # (e.g. 1000207552.jpg, report.pdf)

ChatGPT/                        # projects become subfolders
├── PQR/
│   └── About the PQR study/
│       ├── About the PQR study.md
│       ├── chat.json
│       └── raw.json
└── Unfiled Chat/
    └── ...

.reclaim_manifest.json         # per-provider run manifest (see below)
```

## Markdown file

- Title as `# Heading`, `**Project:**` line when the chat belongs to one
- One `## User` / `## Model` section per turn, in order
- LaTeX preserved verbatim (`$...$`, `$$...$$`)
- Model reasoning/thinking turns are **omitted**; when a turn contained
  thoughts, a structural flag (`_[thought content omitted]_`) marks it —
  no keyword heuristics
- Inline images: `![](media/<file>)`; attachments: `[<name>](media/<file>)`
- Filenames are sanitized and de-duplicated (`file.pdf`, `file_1.pdf`, ...)

## chat.json (canonical dump)

```json
{
  "id": "provider-specific chat id",
  "title": "Chat title",
  "source_url": "https://...",
  "provider": "aistudio | deepseek | kimi | chatgpt",
  "project": "PQR or null",
  "had_thoughts": true,
  "errors": [],
  "turns": [
    {
      "role": "user | model",
      "text": "...",
      "thought": false,
      "images": ["media/1000207552.jpg"],
      "attachments": [{"filename": "report.pdf", "kind": "document"}]
    }
  ]
}
```

## raw.json

The provider's raw response for the chat, recursively scrubbed: strings
longer than 200k chars and media/base64 payloads are replaced with
placeholders. Never contains credentials; time-limited signed URLs are
stripped. Purely an insurance artifact for recovering from parser bugs.

## Manifest

`.reclaim_manifest.json` (one per provider folder) records the run:

```json
{
  "provider": "chatgpt",
  "started": "2026-07-25T...Z",
  "duration_s": 123.4,
  "totals": {"ok": 38, "failed": 0, "skipped": 0},
  "chats": [{"id": "...", "title": "...", "status": "ok", "dir": "PQR/..."}]
}
```

The per-provider sync state (`.last_sync_<provider>.json`) is what the default
update mode reads to skip chats that haven't changed; `reclaim status` reads
this manifest for the archive overview.
