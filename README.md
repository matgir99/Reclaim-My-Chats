# Scrape Webpages

Scrapers for AI chat platforms — extract full conversation history (text, images, documents) into local markdown files.

## Structure

```
Scrape_Webpages/
├── Google AI Studio/     # scraper + output for aistudio.google.com
│   ├── scrape_googleaistudio.py
│   └── <chat folders>/
├── Deepseek Chat/        # scraper + output for chat.deepseek.com
│   ├── scrape_deepseek.py
│   └── <chat folders>/
├── Kimi Chat/            # (placeholder)
└── .playwright-profile/  # shared browser profile (cookies, sessions)
```

## Setup

```bash
pip install --break-system-packages playwright
python3.14 -m playwright install chromium
```

## Google AI Studio

Scrapes all conversations from your library. Extracts full markdown text (user prompts + model responses), filters model thoughts, and captures embedded images as PNG screenshots.

```bash
cd "Google AI Studio"
python3.14 scrape_googleaistudio.py              # full library
python3.14 scrape_googleaistudio.py --url <URL>  # single chat
```

See `Google AI Studio/README.md` for details.

## DeepSeek Chat

Scrapes all conversations from your sidebar. Extracts markdown text with thinking
blocks omitted.

```bash
cd "Deepseek Chat"
python3.14 scrape_deepseek.py              # full library
python3.14 scrape_deepseek.py --url <URL>  # single chat
```

See `Deepseek Chat/README.md` for details.
