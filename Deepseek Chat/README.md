# DeepSeek Chat Scraper

Extracts all conversations from [chat.deepseek.com](https://chat.deepseek.com/) into local markdown files.

## How it works

**IndexedDB-based extraction** — DeepSeek stores all chat history in the browser's IndexedDB (`deepseek-chat` database, `history-message` store). Each record contains the full conversation in raw markdown with proper LaTeX delimiters (`$...$`, `$$...$$`), fragment types, and citation metadata with URLs.

This is the clean download path — equivalent to Google AI Studio's RPC interception approach. No DOM scraping needed.

### Data structure

Each IndexedDB record:
- `data.chat_session` — chat metadata (id, title, model_type, updated_at)
- `data.chat_messages[]` — message array, each with `fragments[]`:
  - `REQUEST` — user message text
  - `THINK` — model reasoning (with elapsed time)
  - `RESPONSE` — final answer in raw markdown
  - `SEARCH` — search queries + results with citation URLs

## Usage

```bash
# Full library (reads from IndexedDB)
python3.14 scrape_deepseek.py

# Single chat (looks up by UUID in IndexedDB)
python3.14 scrape_deepseek.py --url "https://chat.deepseek.com/a/chat/s/..."
```

## Output format

```
<chat title>/
└── <chat title>.md    # Full conversation in markdown
```

Markdown structure:
```markdown
# Chat Title
**Source:** https://chat.deepseek.com/a/chat/s/...
**Scraped:** 2026-07-24 00:25:12
---

## User
User prompt text...

---

<details>
<summary>Thinking</summary>
Thought for 11 seconds
Read 10 web pages
Model reasoning with citations...
</details>

## Assistant
Model response with proper $\\LaTeX$ formulas and citations...
```

## Limitations

- **Authentication** uses the shared `.playwright-profile/` (persistent browser cookies). Must be logged in at least once.
- **IndexedDB** is local — only chats that have been loaded in the browser are available.
- **Citations** are mapped to URLs from SEARCH fragments; not all chats have search enabled.
