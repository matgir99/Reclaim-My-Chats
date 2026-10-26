# Provider compatibility review

Reviewed on 26 October 2026. This records source-backed compatibility work,
not a claim that every feature of the live websites has been tested.

## What these projects provide

Reclaim My Chats has its own provider implementations. These exporters are
references for endpoints and formats, not packages automatically installed
or updated inside our app. Updating Playwright alone does not fix changes
to a chat service's data format.

**chatgpt-exporter is a separate browser extension**, not an export button
provided by ChatGPT. Its recent interactive-reply fix uses a Markdown field
in message metadata. Our native provider now reads that field directly.
Copying replies, transferring chats to Codex, and waiting for an account
export ZIP are unnecessary for this path.

## Sources inspected

Links pin the inspected revision. All projects in this table have an MIT
license. Some findings are existing upstream capabilities we had missed,
rather than newly released changes.

| Project and inspected revision | Findings and decision |
|---|---|
| [chatgpt-exporter, 8404ad9](https://github.com/pionxzh/chatgpt-exporter/tree/8404ad9c6fbee732b2c37759550fd3f02d1543f7) | Recent interactive Markdown fallback; nullable list totals; workspace headers and upload metadata. Adopted these formats. Our newer download endpoint was already aligned. |
| [Kept, c81d59f](https://github.com/egroup-labs/kept/tree/c81d59f956fad8579f9b937ea01fb7cef2b2f2a1) | Claude offset/cursor envelopes; Gemini list cursors, response records and newest-first turns; larger Gemini/Kimi read windows. Adopted. Kimi token storage was already aligned. |
| [HAEVN, ca8b2c0](https://github.com/aiamblichus/haevn/tree/ca8b2c050d0b309f88d096e99ef87706c1583b47) | DeepSeek history API, fragment/direct-content formats and parent IDs. Adopted API reads and branch parsing; its unpinned-only listing was insufficient by itself. |
| [deepseek-mindtrace, 1a475a8](https://github.com/Lumjiel/deepseek-mindtrace/blob/1a475a8ac1cf8b2e429159426f34df49a6c6d594/export_api.py) | October reference for separate pinned/unpinned listings and timestamp cursors. Adopted both listings; retained cache discovery because this source reports older-history limits. |
| [better-deepseek, 62f40d1](https://github.com/EdgeTypE/better-deepseek/tree/62f40d14c4a78a2ee93ccf81b973c1c59e3a9cd3) | October source still intercepts the same history endpoints. Confirms API/cache shapes; its prompt and network-modification features are outside our archiving scope. |
| [DeepSeek Chat Exporter, 7468962](https://github.com/blueberrycongee/DeepSeek-Chat-Exporter/tree/7468962f448f29f8a291cfac959f411ece69c5b4) | Raw Markdown extraction remains useful; DOM/theme-specific fixes do not apply to our API reader. No selector changes needed here. |
| [scrapemychats, 628e334](https://github.com/conradqh/scrapemychats/tree/628e3341a01751935ca259e5b7bba1490c779576) | Confirmed tree and attachment-export formats. Existing import remains compatible. Full sandbox/tool-output recovery is separate work. |
| [ctk, dcf2c70](https://github.com/queelius/ctk/tree/dcf2c70bdfee94a3cfbaab27f87be73994e768d4) | Recent importer work reinforces separate reasoning/content handling and safe traversal. Fixed answer loss and guarded cyclic trees here. |
| [claude-exporter, 2f9e443](https://github.com/glebmish/claude-exporter/tree/2f9e443a2995cf2f52a63c11f6f8c38d401487e4) | Confirms active lineage and separate thinking blocks. Also documents sandbox/research artifacts, which our exporter does not yet recover fully. |
| [gemini-usage, ed68b31](https://github.com/EricAndrechek/gemini-usage/tree/ed68b31576fd4511b41b9413a079b38867ed61f1) | Confirms RPC IDs, embedded tokens, nested result records, and separate answer/thinking fields. Retained authentication and corrected decoding. |

No AGPL AI Studio or Gemini exporter source was studied or incorporated in
this review. See [notices and attribution](../NOTICE.md).

## Changes made here

- **Visible answers:** Claude, Gemini and Kimi could mark a whole answer as
  reasoning when both appeared in one message. Final answers now remain in
  Markdown and `chat.json`; removed reasoning has a separate metadata flag.
- **ChatGPT:** interactive Markdown and code-content replies work in native
  capture and offline imports. Pagination no longer trusts nullable or
  underestimated totals. Metadata-only uploads are downloaded, PDF extensions
  are repaired, and selected workspaces apply to API and file metadata calls.
- **Claude:** support offset lists, cursor envelopes and legacy direct text.
  A cyclic parent chain cannot hang the parser.
- **Gemini:** decode the matching `wrb.fr` result rather than assuming the
  third transport frame. Follow list cursors, recognize current turn metadata
  and restore chronological order. Raise the read window from 100 to 10,000
  turns, matching Kept's reference.
- **DeepSeek:** list pinned and unpinned server chats and supplement their
  IDs with cached discoveries. Fetch messages only after selection and
  unchanged checks. Handle direct content and active branches as well as
  cached fragments. Missing IndexedDB stores no longer hang the reader or
  create an empty database. The direct CLI command can show the login window.
- **Kimi:** retain answers beside thinking blocks, accept string text blocks
  and exclude tool/system messages. Raise the read window from 200 to 1,000
  messages, matching Kept's reference.
- **Failures:** repeated pagination cursors and exhausted read windows fail
  visibly. Unrecognized/empty conversation responses do not create successful
  empty archives or advance their sync records.

Previously saved chats may need **one rebuild** to recover answers omitted
by the old parser: normal updates skip chats whose provider timestamp has
not changed. Use Rebuild for Claude, Gemini and Kimi in the GUI, or run
`reclaim claude --rebuild`, `reclaim googlegemini --rebuild`, and
`reclaim kimi --rebuild`. Archives remain local.

## Verification and privacy

Synthetic regressions check actual saved Markdown/JSON. A separate disposable
Chromium suite executes page-side requests, intercepting every page request
with synthetic responses. It checks personal/workspace headers, file metadata
headers, DeepSeek token lookup and missing/existing IndexedDB stores.

```bash
python -m unittest discover -s tests -q
python -m playwright install --only-shell chromium
python -m unittest discover -s tests -p browser_protocols.py -v
ruff check reclaim tests scripts flet_integration
pyright reclaim tests scripts flet_integration
```

CI and release preparation run both suites. Browser tests use a fresh
temporary context, never the user's saved profile. No upstream extension
was installed or executed. Fixtures contain invented messages and credentials.
Real archives, profiles, configuration and logs remain ignored and must never
be added to a public commit or release asset.

## What still needs live verification

- Real provider accounts were not used. Logins, rate limits, workspaces and
  attachment downloads need acceptance testing with the owner's local accounts.
  Keep captured responses private.
- DeepSeek may limit older server listings; upstream reports roughly 100
  chats per pinned category. Cached IDs and exact URLs help discover more,
  but do not prove a complete account export. The app reports this limit
  when listing. Non-progressing pages are errors.
- Kimi pagination beyond its read window has not been established. A
  continuation token or full 1,000-message response is an error, rather than
  a successful partial archive. Gemini likewise refuses a response filling
  its 10,000-turn window.
- Claude research artifacts, sandbox files and tool outputs; Gemini images,
  citations and deep-research artifacts; and newer AI Studio features need
  separate evidence and end-to-end checks. No new AI Studio RPC changes were
  justified by the MIT sources inspected.
- Reasoning is omitted from normalized Markdown/JSON where identifiable.
  Optional `raw.json` retains the provider response except oversized media,
  and can contain reasoning. See [the output contract](OUTPUT.md).

## Runtime and bot updates

Playwright uses a version range rather than vendored provider code. Flet
remains an optional pinned GUI dependency. Dependabot's existing Flet 1.0.5
PR targets `develop`; its documented fix concerns web/FastAPI startup, not
provider APIs. It and the separate Actions major-version PR remain
independently reviewable. Neither was merged as part of this format review.
