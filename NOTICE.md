# Notices and attribution

ReclaimMyChats is original code published under the MIT License (see
`LICENSE`). It was built *with the help of* the following open-source
projects — by studying their publicly documented techniques (API endpoints,
auth mechanics, data locations) and, where noted, their source code.
Facts and ideas are not copyrightable; no code from these projects is
copied verbatim unless explicitly stated below.

## MIT-licensed projects we learned from

| Project | What we used | License |
|---|---|---|
| [egroup-labs/kept](https://github.com/egroup-labs/kept) | Kimi endpoint/auth research (planned, Phase 1 of ROADMAP) | MIT |
| [aiamblichus/haevn](https://github.com/aiamblichus/haevn) | Provider-architecture and bulk-sync resume design ideas | MIT |
| [blueberrycongee/DeepSeek-Chat-Exporter](https://github.com/blueberrycongee/DeepSeek-Chat-Exporter) | "Extract original markdown from app state instead of rendered HTML" — the principle behind our IndexedDB approach | MIT |
| [pionxzh/chatgpt-exporter](https://github.com/pionxzh/chatgpt-exporter) / [organvm/a-i-chat--exporter](https://github.com/organvm/a-i-chat--exporter) | ChatGPT internal `/backend-api/` knowledge (planned, Phase 2) | MIT |
| [conradqh/scrapemychats](https://github.com/conradqh/scrapemychats) | Confirmation of the real-Chrome bulk-export pattern for ChatGPT | MIT |
| [queelius/ctk](https://github.com/queelius/ctk) | Tree/conversation-format design ideas for importers | MIT |

If we ever copy code (rather than ideas) from any MIT project above, the
relevant file(s) will carry that project's copyright notice and license text
as required.

## Projects we deliberately do NOT use

- [Tokisaki-Galaxy/aistudio-dump-script](https://github.com/Tokisaki-Galaxy/aistudio-dump-script)
  is **AGPL-3.0**. Our Google AI Studio scraper was developed independently
  (RPC replay discovered via network inspection) and is architecturally
  unrelated. No code, and no code-level study, from that project.

## Interoperability (linking, not copying)

- Planned exporter to **HAEVN Markdown** import format, so archives produced
  here can be browsed/searched with HAEVN.
- Planned importer from a **Kept** vault (`~/.kept/vault/`), so Kept can serve
  as the capture path for platforms we don't scrape natively.

These integrations exchange *data files*, not code; no license obligations
beyond those of the data's owner (you).
