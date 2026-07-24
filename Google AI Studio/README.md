# Google AI Studio Scraper

Extracts all conversations from [aistudio.google.com](https://aistudio.google.com/library) into local markdown files with embedded images.

## How it works

**Two-pass architecture:**

1. **RPC interception (fast)** — Google AI Studio loads chat data via an internal `ResolveDriveResource` RPC call. The scraper intercepts this response, which contains full untruncated text (user prompts + model responses) with role metadata. Works for 64/67 chats in the current library.

2. **DOM scrolling (fallback)** — For a few large/image-heavy chats where the RPC response is already cached by the app or too large for the inspector cache, the scraper scrolls through every turn in the virtual scroller, extracts text from `<ms-text-chunk>` elements, and screenshots `<ms-image-chunk>` elements. Adds ~30s per fallback chat.

**Thought filtering:** Model internal reasoning is excluded using a positional heuristic — in a sequence of consecutive model turns before the next user turn, all but the last turn are thoughts. This is more robust than keyword matching.

**Authentication:** Uses a persistent Playwright browser profile (`../.playwright-profile/`, shared with the DeepSeek scraper). Login once, stays logged in. The browser window is launched off-screen and only brought to the foreground if login is required.

## Usage

```bash
# Full library
python3.14 scrape_googleaistudio.py

# Single chat
python3.14 scrape_googleaistudio.py --url "https://aistudio.google.com/prompts/..."

# Custom output directory
python3.14 scrape_googleaistudio.py -o /path/to/output
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

- **Browser window** is kept off-screen during scraping; only shown during login.
- **Images are screenshots**, not original files. The virtual scroller renders images lazily; screenshots capture what's displayed.
- **Document attachments** (Google Drive links, PDFs) are detected but not downloaded — URLs are saved as links.
- **3 DOM-fallback chats** (currently `PQR Brand Design for Finance`, `QNAP TS-233 User Manual Guide`, `MX Keys Business Not Unifying Compatible`) rely on the rendered page, so formatting is slightly less pristine than the RPC path and thought filtering may have minor leakage.
