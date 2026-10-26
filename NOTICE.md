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
| [egroup-labs/kept](https://github.com/egroup-labs/kept) | Kimi endpoint/auth research, implemented vault importer, Claude/Gemini pagination and response formats | MIT |
| [aiamblichus/haevn](https://github.com/aiamblichus/haevn) | Provider architecture, bulk-sync resume ideas and DeepSeek history API/message formats | MIT |
| [blueberrycongee/DeepSeek-Chat-Exporter](https://github.com/blueberrycongee/DeepSeek-Chat-Exporter) | "Extract original markdown from app state instead of rendered HTML" — the principle behind our IndexedDB approach | MIT |
| [pionxzh/chatgpt-exporter](https://github.com/pionxzh/chatgpt-exporter) | ChatGPT API endpoints, session/workspace auth, two-step downloads, Projects and interactive Markdown fallback | MIT |
| [conradqh/scrapemychats](https://github.com/conradqh/scrapemychats) | Confirmation of the real-Chrome bulk-export pattern for ChatGPT | MIT |
| [queelius/ctk](https://github.com/queelius/ctk) | Tree/conversation-format design ideas for importers | MIT |
| [glebmish/claude-exporter](https://github.com/glebmish/claude-exporter) | Claude API documentation, active message lineage and content-block formats | MIT |
| [EricAndrechek/gemini-usage](https://github.com/EricAndrechek/gemini-usage) | Gemini RPC framing, authentication tokens and result-field documentation | MIT |
| [Lumjiel/deepseek-mindtrace](https://github.com/Lumjiel/deepseek-mindtrace) | DeepSeek pinned/unpinned pagination and older-history limit research | MIT |
| [EdgeTypE/better-deepseek](https://github.com/EdgeTypE/better-deepseek) | Confirmation of current DeepSeek history endpoints and cache format | MIT |

The [provider compatibility review](docs/UPSTREAM_REVIEW.md) pins the inspected
upstream revisions and records adopted changes and remaining limits.

If we ever copy code (rather than ideas) from any MIT project above, the
relevant file(s) will carry that project's copyright notice and license text
as required.

## Projects we deliberately do NOT use

- [Tokisaki-Galaxy/aistudio-dump-script](https://github.com/Tokisaki-Galaxy/aistudio-dump-script)
  is **AGPL-3.0**. Our Google AI Studio scraper was developed independently
  (RPC replay discovered via network inspection) and is architecturally
  unrelated. No code, and no code-level study, from that project.

## Interoperability (linking, not copying)

- **HAEVN Markdown exporter** (`reclaim export haevn-md`): archives produced
  here can be browsed/searched with HAEVN. Ships data files only.
- **Kept vault importer** (`reclaim import kept`, `~/.kept/vault/`): Kept
  serves as the capture path for platforms we don't scrape natively.
  Ships data files only.

These integrations exchange *data files*, not code; no license obligations
beyond those of the data's owner (you).

## Runtime dependencies

- [Playwright](https://github.com/microsoft/playwright) (Apache-2.0) — the
  base package's direct Python runtime dependency.
- [Flet](https://github.com/flet-dev/flet) (Apache-2.0), including its desktop
  host — optional GUI dependencies, included in standalone desktop releases.

These packages have their own transitive dependencies. We call their public
APIs; no dependency source is vendored or modified.
