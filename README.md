# ReclaimMyChats

Reclaim your AI chat history. Bulk-export conversations from AI chat platforms into clean local Markdown folders — full text with LaTeX intact, model thoughts filtered out, original-quality images, and real downloaded attachments.

- `STATUS.md` — current development status + task tracker
- `docs/PLAN.md` — master plan (start to end)
- `docs/ARCHITECTURE.md` — hub-and-spoke design rationale
- `docs/research/ECOSYSTEM.md` — survey of existing open-source export tools
- `docs/research/ANALYSIS.md` — critical analysis: best tool per platform
- `NOTICE.md` — attribution and licensing notes (MIT)

## How it works

One program, per-provider modes of two kinds — **native scrapers** where we
are best-in-class, **importers** over the best third-party captures
everywhere else (see `docs/ARCHITECTURE.md`):

| Provider | Mode | How |
|---|---|---|
| Google AI Studio | `scrape` | Replays the app's own `ResolveDriveResource` RPC (SAPISIDHASH auth) — text, structural thought flags, inline original images, Drive attachment downloads |
| DeepSeek | `scrape` | Reads the `deepseek-chat` IndexedDB directly — raw markdown, citations mapped, thinking skipped |
| ChatGPT | `import` | Official `conversations.json` export (tree linearized), or scrapemychats dirs (includes media) |
| Kimi, Claude, Grok, Gemini | `import` | Kept vault (`~/.kept/vault`) — install Kept, sync, import |
| Google AI Studio (offline) | `parse` | Drive folder download / Takeout zip — no browser needed |

Output contract per chat: `<Provider>/<title>/<title>.md` + `chat.json`
(canonical dump) + media files. Thoughts omitted, filenames de-duplicated.

## Setup

```bash
pip install --break-system-packages playwright
python3.14 -m playwright install chromium   # or use system Chrome (default)
```

## Quickstart

```bash
# Google AI Studio (first run opens a window for Google login)
python3.14 -m reclaim scrape aistudio                # full library
python3.14 -m reclaim scrape aistudio --resume       # incremental (skips unchanged)
python3.14 -m reclaim scrape aistudio --only qnap    # title filter

# DeepSeek
python3.14 -m reclaim scrape deepseek --resume

# Detached weekly runs (PID + log, survives terminal close)
./run.sh aistudio --resume     # ./run.sh status | stop

# AI Studio offline (no browser): download the "Google AI Studio" folder
# from drive.google.com (or a Takeout zip), then
python3.14 -m reclaim parse aistudio --from-folder ~/Downloads/"Google AI Studio" \
    --titles titles.json        # optional drive_id -> title map

# ChatGPT: export at chatgpt.com (Settings → Data Controls → Export), then
python3.14 -m reclaim import chatgpt ~/Downloads/conversations.json
# or, with media/files (recommended): run scrapemychats first, then
python3.14 -m reclaim import scrapemychats ~/path/to/scrapemychats/export

# Kimi / Claude / Grok / Gemini: install Kept (docs/providers/kimi.md),
# sync in your browser, then
python3.14 -m reclaim import kept ~/.kept/vault --providers kimi

# Push the whole archive into HAEVN's search UI
python3.14 -m reclaim export haevn-md . archive.zip
```

Backwards-compatible entry points still work:
`Google AI Studio/scrape_googleaistudio.py`, `Deepseek Chat/scrape_deepseek.py`.

## Development

```bash
./run_tests.sh          # 31 offline tests (fixtures, no browser)
```

```
reclaim/
├── __main__.py            # unified CLI: scrape | parse | import | export
├── core/                  # model, writer, browser, manifest
├── providers/             # aistudio, aistudio_files, deepseek,
│                          # chatgpt_import, kept_vault
└── exporters/             # haevn_md
docs/                      # plan, architecture, research, provider notes
tests/                     # fixtures + offline unit tests
```
