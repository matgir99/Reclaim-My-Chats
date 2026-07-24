# Architecture: hub-and-spoke pipeline, not a merge

ReclaimMyChats is **one program with per-provider operating modes** — but it
deliberately does NOT re-implement every provider's scraper, and it does NOT
merge third-party codebases. Acquisition is delegated to the best tool per
provider; we own the normalization core and the output contract.

## Why not a merge

| | ReclaimMyChats | HAEVN | Kept |
|---|---|---|---|
| Language | Python | TypeScript | TypeScript + Rust |
| Runtime | CLI + Playwright | Chrome MV3 extension | Extension + Tauri desktop |
| Storage | plain files | IndexedDB/OPFS archive | Markdown vault + SQLite |

Merging = rewriting two of the three, then maintaining all of it forever.
Their value (adapters, UIs) stays usable *as tools*; our value (capture
fidelity, simple files) stays ours.

## Why not a monolith of per-provider scrapers

That is rebuilding HAEVN/Kept in Python and inheriting every provider's
maintenance burden (ChatGPT throttling, Kimi auth, …). The analysis
(`docs/research/ANALYSIS.md`) concluded: compete on nothing, interoperate on
files.

## The pipeline

```
ACQUIRE (best tool per provider)        NORMALIZE (our core)            ARCHIVE / EXPORT

AI Studio ── our RPC-replay scraper ─┐
  (or Drive folder / Takeout zip)    │
DeepSeek ── our IndexedDB scraper ───┤
                                     │    ┌─────────────────────┐
ChatGPT ── scrapemychats export  ────┤    │  reclaim (one CLI)  │    <Provider>/<chat>/
  (or official conversations.json)   ├───▶│                     │───▶ <chat>.md
                                     │    │  scrape / parse /   │    image_N.png
Kimi ──── Kept extension vault ──────┤    │  import / export    │    attachments…
                                     │    └─────────────────────┘
Claude/Grok/Gemini ─ Kept vault ─────┘         export: HAEVN md / zip (optional)
```

Two kinds of provider modes:

- **`scrape`** — our native scrapers (AI Studio RPC replay, DeepSeek
  IndexedDB). Best-in-class, already built.
- **`parse` / `import`** — thin offline converters over third-party
  captures: Drive/Takeout JSON, `conversations.json`, scrapemychats export
  dirs, Kept vault. ~100 lines each, fixture-testable, near-zero
  maintenance.

## Repository layout (target)

```
reclaim/
├── __main__.py            # one CLI: scrape | parse | import | export
├── core/
│   ├── model.py           # Chat / Turn / Attachment dataclasses
│   ├── writer.py          # canonical folder writer (md + media + name dedup)
│   ├── browser.py         # profile launch, off-screen login, run helpers
│   └── manifest.py        # run manifests, resume bookkeeping
├── providers/
│   ├── aistudio.py        # scrape: RPC replay (today's scraper moves here)
│   ├── aistudio_files.py  # parse: Drive folder / Takeout JSON (offline)
│   ├── deepseek.py        # scrape: IndexedDB (today's scraper moves here)
│   ├── chatgpt_export.py  # import: official conversations.json
│   ├── scrapemychats.py   # import: scrapemychats per-chat export dirs
│   └── kept_vault.py      # import: ~/.kept/vault/* (Kimi, Claude, Grok, …)
└── exporters/
    └── haevn_md.py        # optional: archive -> HAEVN Markdown import spec
```

## CLI contract

```bash
python -m reclaim scrape aistudio [--resume] [--only X] [--keep-raw]
python -m reclaim scrape deepseek  [--resume]
python -m reclaim parse  aistudio --from-folder <dir-of-Drive/Takeout-json>
python -m reclaim import chatgpt <conversations.json>
python -m reclaim import kept <vault-dir> [--providers kimi,claude]
python -m reclaim export haevn-md <out.zip>
```

## Design rules

1. **One output contract**: per-chat folder with `<title>.md` + media +
   attachments, thoughts filtered, names de-duplicated. Every mode produces
   exactly this.
2. **Raw-first**: every mode can persist the provider's raw data
   (`--keep-raw`, or the input file itself for imports) so parsers re-run
   offline and regressions are fixture-testable.
3. **Modes are isolated and dumb**: each provider module is acquire→model;
   `core/writer.py` is the only place that writes files.
4. **Replaceable edges**: any acquisition path can be swapped (RPC replay →
   Drive folder; Kept → native Kimi) without touching the core.
5. **No third-party code in the repo**: integrations exchange data files
   (MIT-compatible, zero obligations). See `NOTICE.md`.

## Migration path (incremental, nothing breaks)

1. Move shared code from the two scrapers into `reclaim/core/` (identical
   behavior; existing entry points keep working as thin wrappers).
2. Add `reclaim/__main__.py` CLI dispatching to providers.
3. Add importer modes one at a time (kept_vault first — covers Kimi +
   Claude + Grok; then chatgpt_export; then aistudio_files).
4. Optional: `exporters/haevn_md.py` when a search UI is wanted.
