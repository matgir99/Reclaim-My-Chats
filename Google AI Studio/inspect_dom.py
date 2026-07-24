#!/usr/bin/env python3.14
"""Inspect Google AI Studio DOM and save rendered HTML structure to a file."""

import json
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

import scrape_googleaistudio as sc

USER_DATA_DIR = str(Path(__file__).resolve().parent.parent / '.playwright-profile')
OUT_FILE = Path(__file__).resolve().parent / 'inspect_dom_output.json'
LOG_FILE = Path(__file__).resolve().parent / 'inspect_dom.log'


def log(msg):
    line = f'{time.strftime("%Y-%m-%d %H:%M:%S")} {msg}'
    print(line)
    with open(LOG_FILE, 'a', encoding='utf-8') as f:
        f.write(line + '\n')


def main():
    OUT_FILE.unlink(missing_ok=True)
    LOG_FILE.unlink(missing_ok=True)

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=USER_DATA_DIR,
            headless=False,
            executable_path='/usr/bin/google-chrome-stable',
            args=[
                '--disable-blink-features=AutomationControlled',
                '--disable-features=AutomationControlled',
                '--disable-dev-shm-usage',
                '--window-position=-3000,-3000',
            ],
            viewport={'width': 1400, 'height': 900},
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        sc.ensure_logged_in(page)
        log('Login OK / library loaded')

        chats = sc.get_chat_list(page)
        log(f'Found {len(chats)} chats in library')

        # Inspect several chats with potentially different formatting
        chat_titles = ['Circular', 'Building a NAS', 'GRE Prep', 'QNAP', 'PQR']
        chats_to_inspect = []
        for title in chat_titles:
            matches = [c for c in chats if title.lower() in (c.get('title', '') or '').lower()]
            if matches:
                chats_to_inspect.append(matches[0])
        if not chats_to_inspect:
            chats_to_inspect = [chats[0]]

        all_info = []
        for chat in chats_to_inspect:
            log(f'Inspecting chat: {chat["title"]}')
            page.goto(chat['url'], wait_until='domcontentloaded', timeout=60000)
            time.sleep(5)

            info = page.evaluate('''() => {
                const turns = document.querySelectorAll('.chat-turn-container');
                const R = [];
                for (let i = 0; i < Math.min(turns.length, 6); i++) {
                    const t = turns[i];
                    const role = t.className.toLowerCase().includes('user') ? 'user' : 'model';
                    const chunks = t.querySelectorAll('ms-text-chunk');
                    const chunkData = [];
                    for (let j = 0; j < Math.min(chunks.length, 3); j++) {
                        const c = chunks[j];
                        chunkData.push({
                            outerHTML: c.outerHTML.substring(0, 8000),
                            dataAttrs: Object.keys(c.dataset).reduce((a, k) => { a[k] = c.dataset[k].substring(0,200); return a; }, {}),
                            hasShadow: !!c.shadowRoot,
                            shadowHTML: c.shadowRoot ? c.shadowRoot.innerHTML.substring(0, 3000) : null,
                        });
                    }
                    R.push({role, chunkData});
                }
                return R;
            }''')
            all_info.append({'title': chat['title'], 'url': chat['url'], 'turns': info})

        with open(OUT_FILE, 'w', encoding='utf-8') as f:
            json.dump(all_info, f, indent=2, ensure_ascii=False)
        log(f'Saved DOM inspection to {OUT_FILE} ({len(all_info)} chats)')

        ctx.close()


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        with open(LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(f'ERROR: {e}\n')
            import traceback
            traceback.print_exc(file=f)
        raise
