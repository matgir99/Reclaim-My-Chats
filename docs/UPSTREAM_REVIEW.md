# Provider compatibility review

Reviewed on 26 October 2026 and updated on 8 November 2026. This records
source-backed compatibility work, not a claim that every feature of the live
websites has been tested.

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
- DeepSeek's reported limit of roughly 100 chats per pinned category was
  the separate pinned pass repeating its first page (see the November
  update). A single pass listed all 105 chats of a live account. Cached IDs
  and exact URLs remain supplements. Non-progressing pages are errors.
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

## November 2026 update

Every source above was compared with its default branch on 8 November.
Eight were unchanged. Two had new commits:

| Project and inspected revision | Findings and decision |
|---|---|
| [chatgpt-exporter, 44affd4](https://github.com/pionxzh/chatgpt-exporter/tree/44affd4d68c88f73be59f8adda521da08ba95ba0) | Inline `url`, `entity` and `image_group` markers in interactive replies; residual private-use markers; official JSON messages without a recipient. Adopted, see below. Image, ZIP and userscript changes do not apply. |
| [better-deepseek, df0c9a1](https://github.com/EdgeTypE/better-deepseek/tree/df0c9a1867f112069cc0d6ecd26509ba8e7a8a5d) | Prompt modules, skills, web search, MCP and UI work only. No history endpoint or cache change, so nothing to adopt. |

**ChatGPT citation markers.** Replies contain private-use markers such as
`\ue200cite\ue202turn0search3\ue201`, described in
`metadata.content_references`. The parser did not resolve them, so saved
Markdown showed text like `citeturn0search3`. They now become what ChatGPT
displays: web citations become links, file citations the file name,
entities their name, hidden and interactive-only markers nothing, and a
source footnote a **Sources** list. Unlike upstream, ordinary reply text is
not normalized, so dashes and non-breaking spaces are kept. A `url` marker
that holds a search reference instead of an address takes its link from
the matching reference. Unreferenced markers are removed whole rather
than leaving their inner text.

**ChatGPT tool calls.** The October change that renders `code` content also
rendered messages addressed to tools: web searches, code runs, plan and
memory updates. In a local test over 42 stored ChatGPT responses, 674 of
1,476 produced turns were such calls. A message whose recipient is set and
is not `all` is now skipped, matching upstream. A missing recipient, as in
some official exports, remains visible. Tool calls stay in `raw.json`.

**ChatGPT workspace cookie.** A live check on 8 November found the
`_account` cookie set to `personal`, while the accounts check keys accounts
by account id and `default`. The exact-key lookup failed, stopping every
listing. The cookie now matches an accounts key, an account id, or, for
`personal`, the account whose structure is `personal`. Exactly one account
must match; an unknown workspace still stops before any chat is fetched.
Upstream instead falls back to sending no workspace header.

**ChatGPT rate limits.** A live update of 238 chats received HTTP 429
for its last few chats. Rate-limited requests waited only 15 seconds in
total and then failed with the message `failed: None`. They now honor
`Retry-After`, otherwise wait 15, 30, 60 and 120 seconds, and report
HTTP 429. Failed chats keep their old sync record, so the next update
retries them.

**Renamed and moved chats (all providers).** A chat folder is named after
the chat title and, for ChatGPT, its project. Renaming a chat, moving it
into a project or renaming the project created a new folder and left the
old one as a stale duplicate; the live update produced 23. When a chat is
saved in a new folder, its previous folder is now retired: its Markdown,
`chat.json` and `raw.json` are removed, media missing from the new folder
is moved there, and identical media is removed. A folder shared with
another chat, containing subfolders or holding conflicting media is kept.
Sync records now store paths relative to the provider folder; older
absolute records are rebased by the provider folder name, so a moved
archive still resolves.

**ChatGPT `--url`.** A chat fetched by URL had no project and was saved
outside its project folder. Its project is now taken from the
conversation's `gizmo_id`. As before, `--url` skips a chat that is already
archived unless `--rebuild` is given.

**Kimi moved to kimi.ai.** Kimi now serves accounts outside mainland
China at kimi.ai, with Google or non-+86 phone sign-in; kimi.com shows a
notice sending those users there. Both run the same web app and `/apiv2`
API, and a live account kept its chat ids. The provider now defaults to
`https://www.kimi.ai`; set `RECLAIM_KIMI_URL=https://www.kimi.com` for a
mainland account. The site stores its token separately, so the first run
asks for a new login. The last listing page is now `{}`, because protobuf
JSON omits empty fields; it ends the listing instead of failing it.
Re-parsing 80 previously saved Kimi responses found no missing answers.

**DeepSeek listing.** The listing cursor is the last row's position,
`(pinned, updated_at)`, in a list sorted pinned-first. The October code ran
a second pass with `pinned=true`; for an unpinned time that request
restarts the unpinned section, so it returned the same 100 chats with
`has_more` forever and every live run failed. The provider now makes one
pass starting at `pinned=true` and takes each cursor's pinned flag from the
last row. Re-parsing 100 previously saved responses found no missing answers.

**Google sign-in (AI Studio and Gemini).** Login completion was detected
with the glob `**/aistudio.google.com/**`, which also matches Google's
account chooser because its address carries `?continue=https://aistudio…`.
An expired session therefore reported "Logged in!" at once, and requests
then failed with HTTP 401. Completion now requires the page's own host.

**Claude with a saved session.** The provider opened claude.ai only when
the session cookie was missing. With a saved session the page stayed on
`about:blank`, and every page-side request failed with `Failed to fetch`;
only runs that needed a login worked. It now opens claude.ai whenever the
page is elsewhere, which also covers `reclaim all`. A live account listed
its chat organization; its API-console organization has no chats (HTTP 403).

ChatGPT archives saved before this change need **one rebuild** to remove
the markers: `reclaim chatgpt --rebuild`, or Rebuild in the GUI.

## Weekly check

`scripts/upstream_watch.py` compares every project linked above with the
revision last reviewed here, and the weekly workflow
`.github/workflows/upstream-watch.yml` opens or updates one issue when a
project has new commits. Record each review below with a link pinned to the
reviewed revision; the latest link of a project counts.

**9 November 2026.** The first run found three new commits in
[chatgpt-exporter, 023cf2b](https://github.com/pionxzh/chatgpt-exporter/tree/023cf2b1ece2d41d9be326c026f7ae6ae10c9ae5).
Adopted for `reclaim import chatgpt`: an official export split across
`conversations-000.json`, `conversations-001.json` and so on is read from the
export folder or ZIP, a conversation found twice keeps its latest version,
and thinking preambles are left out (the export drops their flag and the
message recipient, so a reply directly followed by a reasoning step counts as
one). Scheduled-task runs stay separate replies here already, since the
parser never merges consecutive replies.

## Runtime and bot updates

Playwright uses a version range rather than vendored provider code. The
November checks ran with Playwright 1.64.1 and Chrome 157. Flet remains an
optional pinned GUI dependency. Dependabot's Flet 1.0.5 and Actions
major-version updates were merged into `develop` after their checks passed.
Flet 1.1 was released days before this review; it stays for a later update.
