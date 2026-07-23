# DeepSeek Chat Scraper

Extracts all conversations from [chat.deepseek.com](https://chat.deepseek.com/) into local markdown files.

## How it works

**DOM-based extraction** — DeepSeek Chat is a SPA (single-page application). Messages are identified by CSS class patterns:

- **User messages**: `.d29f3d7d.ds-message` with text in `.fbb737a4`
- **Assistant thinking**: `div._74c0879` (DeepSeek-R1 reasoning)
- **Assistant answer**: `div.ds-markdown.ds-assistant-message-main-content` (final response)

The scraper scrolls through the entire chat to trigger the virtual scroller to load all messages, then extracts text from each turn. Thinking blocks are wrapped in `<details>` tags (collapsed by default).

## Usage

```bash
# Full library
python3.14 scrape_deepseek.py

# Single chat
python3.14 scrape_deepseek.py --url "https://chat.deepseek.com/a/chat/s/..."

# Include thinking inline (not collapsed)
python3.14 scrape_deepseek.py --include-thinking
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
**Scraped:** 2026-07-23 19:50:02

---

<details>
<summary>Thinking</summary>
Model reasoning...
</details>

## Assistant
Model response...

---

## User
User prompt...

---
```

## Limitations

- **Images** are detected but rarely present in DeepSeek chats (text-focused platform).
- **Thinking/Reasoning** is collapsed in `<details>` by default; use `--include-thinking` to show inline.
- **Authentication** uses the shared `.playwright-profile/` (persistent browser cookies).
