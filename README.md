# ReclaimMyChats

Reclaim your AI chat history. Bulk-export conversations from AI chat platforms into clean local Markdown folders — full text with LaTeX intact, model thoughts filtered out, original-quality images, and real downloaded attachments.

## Structure

```
ReclaimMyChats/
├── Google AI Studio/     # scraper + output for aistudio.google.com
│   ├── scrape_googleaistudio.py
│   └── <chat folders>/
├── Deepseek Chat/        # scraper + output for chat.deepseek.com
│   ├── scrape_deepseek.py
│   └── <chat folders>/
├── Kimi Chat/            # (planned — see ROADMAP.md)
└── .playwright-profile/  # shared browser profile (cookies, sessions)
```

## Setup

```bash
pip install --break-system-packages playwright
python3.14 -m playwright install chromium
```

## Google AI Studio

Scrapes all conversations from your library by **replaying the app's own
`ResolveDriveResource` RPC** from page context — no DOM scraping, no cache
eviction, works for every chat regardless of size. Extracts raw markdown
(user prompts + model responses, LaTeX preserved), filters model thoughts via
a structural flag, saves inline images at original quality, and downloads
user-uploaded Drive attachments with their real filenames.

```bash
cd "Google AI Studio"
python3.14 scrape_googleaistudio.py              # full library
python3.14 scrape_googleaistudio.py --url <URL>  # single chat
./run_full.sh start                              # detached run (status|stop)
```

Latest full run: 67/67 chats, 0 failures, 0 fallbacks, ~10 minutes.
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
