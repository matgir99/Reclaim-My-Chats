# Google AI Studio Scraper

Extracts all conversations from [aistudio.google.com](https://aistudio.google.com/library) into local markdown files with embedded images.

## How it works

**Two-pass architecture:**

1. **RPC interception (fast)** — Google AI Studio loads chat data via an internal `ResolveDriveResource` RPC call. The scraper intercepts this response, which contains full untruncated text (user prompts + model responses) with role metadata. Works for ~95% of chats.

2. **DOM scrolling (fallback)** — For chats with very large responses (>1MB) where the RPC body is evicted from Chrome's cache, or for chats containing images, the scraper scrolls through every turn in the virtual scroller, extracts text from `<ms-text-chunk>` elements, and screenshots `<ms-image-chunk>` elements.

**Thought filtering:** Model internal reasoning (marked by bold headers like `**Analyzing...**`, `**Researching...**`, etc.) is detected and excluded from saved markdown.

**Authentication:** Uses a persistent Playwright browser profile (`.playwright-profile/`). Login once, stays logged in.

## Usage

```bash
# Full library
python3.14 scrape_aistudio.py

# Single chat
python3.14 scrape_aistudio.py --url "https://aistudio.google.com/prompts/..."

# Custom output directory
python3.14 scrape_aistudio.py -o /path/to/output
```

## Output format

Each chat saved in its own folder:

```
<chat title>/
├── <chat title>.md    # Full conversation in markdown
├── image_1.png        # Embedded images (if any)
├── image_2.png
└── ...
```

Markdown structure:
```markdown
# Chat Title

**Source:** https://aistudio.google.com/prompts/...
**Scraped:** 2026-07-23 19:33:44

## Images
- ![image](image_1.png)

---

## User
User prompt text here...

---

## Model
Model response text here...

---
```

## Limitations

- **Images are screenshots**, not original files. The virtual scroller renders images lazily; screenshots capture what's displayed.
- **Document attachments** (Google Drive links, PDFs) are detected but not downloaded — URLs are saved as links.
- **Very large responses** (35MB+) trigger the slower DOM fallback (adds ~30s per chat).
