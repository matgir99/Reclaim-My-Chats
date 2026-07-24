# Google AI Studio Scraper

Extracts all conversations from [aistudio.google.com](https://aistudio.google.com/library) into local markdown files with original-quality images and downloaded attachments.

## How it works

**Replay-first architecture.** AI Studio loads each chat via an internal
`ResolveDriveResource` RPC — a plain POST whose body is just `["<chat-id>"]`.
Instead of navigating to each chat and hoping to intercept that response
(the old approach, which broke whenever Chrome evicted huge bodies from its
inspector cache — one chat has a **116 MB** response), the scraper **replays
the RPC itself** from page context, authenticated with a `SAPISIDHASH`
computed from the session cookie, exactly like the app does.

From the RPC JSON it then extracts, with zero DOM involvement:

| Data | Location in turn (36-field list) |
|---|---|
| Text | `turn[0]` |
| Role (`user`/`model`) | `turn[8]` |
| **Thought flag** (reasoning turns, excluded) | `turn[19]` truthy |
| Inline images (base64, original quality) | `turn[12]` = `[mime, b64]` |
| User-uploaded Drive attachment IDs | `turn[1]` = `[file_id, ...]` |
| API-side error marker | `turn[28]` |

**Attachments** are downloaded from `drive/v3/files/<id>?alt=media` using a
Bearer token from a replayed `GenerateAccessToken` call (refreshed on 401 /
every 30 min). Real filenames come from Drive metadata; collisions are
de-duplicated (`immagine.png`, `immagine_1.png`, ...).

**Fallback chain** (practically never needed): RPC replay → response
interception → DOM scrape (cmark HTML → Markdown, element screenshots,
positional thought heuristic).

**Thought filtering** is structural (`turn[19]`), not heuristic: validated
against raw dumps — image-generation chats whose answers legitimately span
multiple turns are kept whole, while genuine thinking turns are dropped.

**Authentication:** persistent Playwright profile (`../.playwright-profile/`,
shared with the DeepSeek scraper). Login once, stays logged in. The browser
window launches off-screen and only appears if login is required.

## Usage

```bash
# Full library
python3.14 scrape_googleaistudio.py

# Single chat / filters / ranges
python3.14 scrape_googleaistudio.py --url "https://aistudio.google.com/prompts/..."
python3.14 scrape_googleaistudio.py --only qnap        # title substring
python3.14 scrape_googleaistudio.py --start 10 --limit 5
python3.14 scrape_googleaistudio.py --resume           # skip already-saved
python3.14 scrape_googleaistudio.py --keep-raw         # also save raw RPC JSON

# Detached full run (survives terminal close; PID + log file)
./run_full.sh start     # ./run_full.sh status | stop
```

## Output format

Each chat saved in its own folder:

```
<chat title>/
├── <chat title>.md     # Full conversation in markdown
├── image_1.png         # Inline model-generated images (original quality)
├── immagine.png        # User-uploaded attachments (real Drive filenames)
└── ...
```

Consecutive same-role turns are merged into one section, so multi-part
answers (text → image → text) stay together, with images referenced inline
at the correct position:

```markdown
# Chat Title

**Source:** https://aistudio.google.com/prompts/...
**Scraped:** 2026-07-24 16:05:00

---

## User

Prompt text...

**Attachment:** [immagine.png](immagine.png)

---

## Model

Answer part 1...

![image_1.png](image_1.png)

Answer part 2...

---
```

LaTeX is preserved verbatim (raw model text, not rendered HTML).

## Current status

Latest full run: **67/67 chats, 0 failures, 0 fallbacks** — every chat via
RPC replay. 36 inline images + 90 Drive attachments downloaded, no thought
leakage, ~10 minutes total (previously ~1 hour with 3 DOM fallbacks).
