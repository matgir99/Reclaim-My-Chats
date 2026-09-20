# Archive output

The default root is `chats/` inside a checkout or the application's data
folder. Set `archive` in `.reclaim.json` or use `-o` on a provider command to
choose another destination. Provider folders have these names:

| Provider command | Folder |
|:--|:--|
| `googleaistudio` | `Google AI Studio` |
| `deepseek` | `Deepseek Chat` |
| `kimi` | `Kimi Chat` |
| `chatgpt` | `ChatGPT` |
| `claude` | `Claude` |
| `googlegemini` | `Google Gemini` |

A chat folder contains a Markdown file named for its title, canonical
`chat.json`, optional media-stripped `raw.json`, and downloaded files under
`media/`. ChatGPT Projects add a project folder. Names are sanitized and
deduplicated.

```text
chats/
├── ChatGPT/
│   ├── Example project/
│   │   └── Example conversation/
│   │       ├── Example conversation.md
│   │       ├── chat.json
│   │       ├── raw.json
│   │       └── media/
│   ├── .last_sync_chatgpt.json
│   └── .reclaim_manifest.json
└── Claude/
    └── ...
```

The Markdown contains turns in order, preserving LaTeX text and local links
to saved media. Provider thought content is omitted where the provider
identifies it; a structural marker can indicate that omission. `chat.json`
contains the normalized turns and source metadata. `raw.json` retains a
scrubbed provider response to help recover from parser errors.

Each provider's `.last_sync_<provider>.json` maps chat IDs to update times and
saved Markdown paths. Default update mode skips entries classified as
unchanged. `.reclaim_manifest.json` records when the last run started, its
duration, per-chat results, and totals such as chats, successes, failures,
images, and documents. The GUI and `reclaim status` read these local records
and files; they do not infer live provider state without contacting it.
