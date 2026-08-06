"""Claude provider (scrape mode) — claude.ai API replay.

Cookie auth only (no bearer token): the `sessionKey` cookie is the
session, `lastActiveOrg` names the org whose id prefixes every API path.
Page-side fetch() attaches the cookies automatically.

  GET /api/organizations/{orgId}/chat_conversations?offset=N&limit=M
  GET /api/organizations/{orgId}/chat_conversations/{uuid}
      ?tree=true&rendering_mode=messages&render_all_tools=true

Endpoints + shapes verified against live traffic by claude-exporter
(glebmish, docs/claude-ai-api.md; Kept commit ddfbcc2 confirms offset
pagination). Every chat carries abandoned edit-branches in
`chat_messages`; the active lineage is recovered by walking
`parent_message_uuid` pointers from `current_leaf_message_uuid` to the
root (fallback: full array, never render nothing). Thinking blocks are
skipped (thought flag on the turn), tool_use/tool_result blocks are
skipped in v1 (known gap — artifacts/research reports live there).
"""

from __future__ import annotations

import argparse
import json
import time
import traceback
from pathlib import Path

from ..core import browser, config
from ..core.manifest import SyncState, print_dry_run, write_manifest
from ..core.model import Attachment, Chat, Turn
from ..core.progress import progress
from ..core.writer import write_chat, write_raw

BASE_URL = 'https://claude.ai'
PROVIDER = 'claude'
LIST_LIMIT = 100

_GET_JS = """async (args) => {
    const resp = await fetch(args.url, {
        headers: {'accept': 'application/json'},
        credentials: 'include',
    });
    const text = await resp.text();
    return {status: resp.status, text};
}"""


def _get(page, url: str, retries: int = 3) -> dict:
    """Page-side GET (cookies attach automatically); returns parsed JSON."""
    last = None
    for attempt in range(retries):
        try:
            r = page.evaluate(_GET_JS, {'url': url})
            if r['status'] == 200:
                return json.loads(r['text'])
            if r['status'] == 404:
                raise RuntimeError('HTTP 404 — chat deleted?')
            if r['status'] in (401, 403):
                raise RuntimeError(f'auth failed ({r["status"]}) — session expired?')
            if r['status'] == 429:
                time.sleep(5 + attempt * 5)
                continue
            last = f'HTTP {r["status"]}: {r["text"][:120]}'
        except RuntimeError:
            raise
        except Exception as e:
            last = e
        time.sleep(2 + attempt * 2)
    raise RuntimeError(f'GET {url[:80]} failed: {last}')


def _cookies(page) -> dict:
    return {c['name']: c['value'] for c in page.context.cookies()}


def ensure_logged_in(page) -> str:
    """Bring the session up, return the org id (uuid).

    Org id from the `lastActiveOrg` cookie; fallback: GET
    /api/organizations and take the first org's uuid. Waits for a human
    login (window shown) if the sessionKey cookie is missing.
    """
    if not _cookies(page).get('sessionKey'):
        try:
            page.goto(BASE_URL, wait_until='domcontentloaded', timeout=60000)
        except Exception:
            pass
        time.sleep(4)
    if not _cookies(page).get('sessionKey'):
        page.bring_to_front()
        browser.set_window_bounds(page, state='normal', left=100, top=100)
        print('\n' + '=' * 50)
        print('  LOG IN to Claude in the browser window.')
        print('  Waiting (up to 15 minutes)...')
        print('=' * 50 + '\n')
        import os
        deadline = time.time() + int(os.environ.get('RECLAIM_LOGIN_TIMEOUT', '900'))
        while time.time() < deadline:
            if _cookies(page).get('sessionKey'):
                print('Logged in! Hiding window...\n')
                browser.set_window_bounds(page, state='minimized')
                time.sleep(2)
                break
            time.sleep(3)
        else:
            raise RuntimeError('Login timeout')
    org = _cookies(page).get('lastActiveOrg')
    if not org:
        data = _get(page, f'{BASE_URL}/api/organizations')
        orgs = data if isinstance(data, list) else data.get('organizations') or []
        if not orgs:
            raise RuntimeError('no organizations in /api/organizations — account issue?')
        org = orgs[0].get('uuid') or orgs[0].get('id')
        if not org:
            raise RuntimeError('unexpected organization shape: '
                               f'{json.dumps(orgs[0])[:200]}')
    return org


def list_chats(page, org: str) -> list[dict]:
    """GET .../chat_conversations, offset-paginated. Normalized items:
    {'id': uuid, 'title': name, 'updated_at': <ISO string>}."""
    items, offset = [], 0
    while True:
        data = _get(page, f'{BASE_URL}/api/organizations/{org}'
                          f'/chat_conversations?offset={offset}&limit={LIST_LIMIT}')
        batch = data if isinstance(data, list) else data.get('data') or []
        if not isinstance(batch, list):
            raise RuntimeError('unexpected list shape: ' + json.dumps(data)[:200])
        for it in batch:
            if not isinstance(it, dict) or not it.get('uuid'):
                continue
            items.append({'id': it['uuid'], 'title': it.get('name') or '',
                          'updated_at': it.get('updated_at')})
        if len(batch) < LIST_LIMIT:
            return items
        offset += len(batch)


# ---------------------------------------------------------------------------
# Conversation parsing (pure, fixture-tested)
# ---------------------------------------------------------------------------
def active_lineage(messages: list[dict], leaf_uuid) -> list[dict]:
    """The current message chain: leaf -> parent pointers -> root, reversed.

    Every Claude chat keeps abandoned edit-branches in `chat_messages`;
    only this lineage is what the user sees. Fallback: full array when
    the leaf is missing (never render nothing).
    """
    by_uuid = {m.get('uuid'): m for m in messages if m.get('uuid')}
    if leaf_uuid and leaf_uuid in by_uuid:
        chain: list[dict] = []
        cur = leaf_uuid
        while cur and cur in by_uuid:
            m = by_uuid[cur]
            chain.append(m)
            parent = m.get('parent_message_uuid')
            if parent == cur:
                break  # self-loop safety
            cur = parent
        chain.reverse()
        return chain
    return list(messages)


def _block_turn(m: dict) -> tuple[str, bool, list[Attachment]]:
    """One message -> (text, had_thinking, attachments)."""
    sender = m.get('sender')
    if sender not in ('human', 'assistant'):
        return '', False, []
    texts, thought = [], False
    for block in m.get('content') or []:
        if not isinstance(block, dict):
            continue
        btype = block.get('type')
        if btype == 'text':
            t = block.get('text') or ''
            if t:
                texts.append(t)
        elif btype == 'thinking':
            thought = True
        # tool_use / tool_result / image blocks: skipped in v1 (documented gap)
    atts: list[Attachment] = []
    for a in m.get('attachments') or []:  # pasted/uploaded files
        if not isinstance(a, dict):
            continue
        fname = a.get('file_name') or 'file'
        text = a.get('extracted_content') or ''
        atts.append(Attachment(filename=fname, kind='document',
                               data=text.encode('utf-8') if text else None,
                               description=fname,
                               source_url=a.get('file_url') or ''))
    for f in m.get('files') or []:  # image uploads (downloaded later)
        if not isinstance(f, dict):
            continue
        url = ((f.get('preview_asset') or {}).get('url')
               or f.get('preview_url') or '')
        fname = f.get('file_name') or 'image'
        atts.append(Attachment(filename=fname, kind='image', data=None,
                               source_url=url, description=fname))
    return '\n\n'.join(texts).strip(), thought, atts


def parse_chat(data: dict, chat_id: str, source_url: str,
               title: str = '') -> Chat:
    """Envelope JSON -> canonical Chat (pure; image bytes are None here)."""
    turns = []
    for m in active_lineage(data.get('chat_messages') or [],
                            data.get('current_leaf_message_uuid')):
        text, thought, atts = _block_turn(m)
        if not text and not atts:
            continue
        sender = m.get('sender')
        turns.append(Turn(role='user' if sender == 'human' else 'model',
                          text=text, thought=thought, attachments=atts))
    name = title or data.get('name') or '(untitled)'
    return Chat(id=chat_id, title=name, source_url=source_url,
                turns=turns, provider=PROVIDER)


def fetch_conversation(page, org: str, conv_id: str, title: str = '') -> tuple[Chat, dict]:
    data = _get(page, f'{BASE_URL}/api/organizations/{org}/chat_conversations/'
                      f'{conv_id}?tree=true&rendering_mode=messages'
                      f'&render_all_tools=true')
    chat = parse_chat(data, conv_id, f'{BASE_URL}/chat/{conv_id}', title)
    # Materialize image uploads (signed preview URLs, page-side).
    for turn in chat.turns:
        for att in turn.attachments:
            if att.kind == 'image' and att.data is None and att.source_url:
                try:
                    resp = page.request.get(att.source_url, timeout=120000)
                    if resp.status == 200 and resp.body():
                        att.data = resp.body()
                except Exception:
                    pass
    return chat, data


def run(page, org: str, items: list[dict], out_dir: Path,
        skip_unchanged: bool = False, save_raw: bool = True,
        log: bool = False) -> list[dict]:
    """Fetch the given conversations. Returns per-chat results for the manifest.

    Per-chat lines print only with log=True (default output is
    summary-level; failures always print)."""
    out_dir = Path(out_dir)
    sync = SyncState(out_dir, PROVIDER)
    results = []
    t_run = time.time()
    for i, item in enumerate(items):
        cid = item.get('id', '')
        label = (item.get('title') or cid)[:55]
        updated = item.get('updated_at')
        if log:
            print(progress(i + 1, len(items), t_run))
        if skip_unchanged and sync.is_unchanged(cid, updated):
            if log:
                print(f'[{i + 1}/{len(items)}] {label} -> skip (unchanged)')
            continue
        t0 = time.time()
        try:
            chat, raw = fetch_conversation(page, org, cid, item.get('title', ''))
            stats = write_chat(chat, out_dir)
            if save_raw:
                try:
                    write_raw(stats['dir'], raw)
                except Exception:
                    pass
            sync.mark(cid, updated, str(stats['md']))
            results.append({'id': cid, 'title': chat.title, 'ok': True,
                            'duration_s': round(time.time() - t0, 1),
                            **{k: stats[k] for k in ('turns', 'chars', 'images', 'docs')}})
            if log:
                extra = f", {stats['images']} img" if stats['images'] else ''
                extra += f", {stats['docs']} doc" if stats['docs'] else ''
                print(f'[{i + 1}/{len(items)}] {label} -> {stats["turns"]}t, '
                      f'{stats["chars"]:,} chars{extra}')
                print(f'    assets: {stats["images"] + stats["docs"]} · '
                      f'{time.time() - t0:.1f}s')
        except Exception as e:
            print(f'[{i + 1}/{len(items)}] {label} -> FAILED: {e}')
            traceback.print_exc()
            results.append({'id': cid, 'title': label, 'ok': False, 'error': str(e)})
        time.sleep(0.4)
    sync.save()
    return results


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog='reclaim claude',
        description='Archive Claude.ai conversations (update by default).')
    ap.add_argument('title', nargs='?',
                    help='fetch chats whose title contains this '
                         '(case-insensitive)')
    ap.add_argument('--rebuild', action='store_true',
                    help='fetch everything, overwriting local copies')
    ap.add_argument('--url', help='fetch one exact chat URL')
    ap.add_argument('--list', action='store_true',
                    help='print chat titles, no download')
    ap.add_argument('--log', action='store_true',
                    help='full per-chat log + verbose progress and timings')
    ap.add_argument('--dry-run', action='store_true',
                    help='preview what would be fetched; nothing downloaded')
    ap.add_argument('--skip', type=int, default=0,
                    help='skip the first N chats in the listing')
    ap.add_argument('--limit', type=int, default=0,
                    help='fetch at most N chats')
    ap.add_argument('--no-raw', action='store_true',
                    help='do not save media-stripped raw.json per chat')
    ap.add_argument('-o', '--output-dir',
                    default=str(config.archive_root() / 'Claude'))
    return ap


def parse_args(argv=None):
    """Parse + validate CLI args (shared by main() and `reclaim all`)."""
    ap = build_parser()
    args = ap.parse_args(argv)
    if args.title and args.url:
        ap.error('pass a title or --url, not both')
    return args


def main(argv=None):
    args = parse_args(argv)
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        ctx, page = browser.launch(p)
        try:
            return run_session(page, args)
        finally:
            ctx.close()


def run_session(page, args) -> int:
    """Provider session on an already-launched page; shared by main()
    (own browser) and `reclaim all` (one browser for all providers)."""
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    started = time.time()
    org = ensure_logged_in(page)
    if args.url:
        cid = args.url.rstrip('/').split('/')[-1].split('?')[0]
        items = [{'id': cid, 'title': '', 'updated_at': None}]
    else:
        print('Listing conversations...')
        items = list_chats(page, org)
        print(f'Found {len(items)} conversations')
    if args.title:
        items = [c for c in items
                 if args.title.lower() in (c.get('title') or '').lower()]
    if args.skip:
        items = items[args.skip:]
    if args.limit:
        items = items[:args.limit]
    if not items:
        print('No chats matched.')
        return 1
    if args.list:
        for n, c in enumerate(items, 1):
            print(f'{n}. {c.get("title") or c["id"]}')
        print(f'-- {len(items)} chat(s)')
        return 0
    skip_unchanged = not args.rebuild and not args.title
    if args.dry_run:
        sync = SyncState(out_dir, PROVIDER, migrate=False)
        updated_map = {c.get('id', ''): c.get('updated_at') for c in items}
        return print_dry_run(items, updated_map, sync, skip_unchanged,
                             log=args.log)

    print(f"\n{'=' * 50}\n  {len(items)} conversations\n{'=' * 50}\n")
    results = run(page, org, items, out_dir, skip_unchanged=skip_unchanged,
                  save_raw=not args.no_raw, log=args.log)
    manifest = write_manifest(out_dir, PROVIDER, results, started)
    ok = sum(1 for r in results if r.get('ok'))
    print(f"\n{'=' * 50}")
    print(f'  Done: {ok} ok, {len(results) - ok} failed')
    print(f'  Manifest: {manifest}')
    print(f"{'=' * 50}")
    return 0 if ok == len(results) else 2


if __name__ == '__main__':
    raise SystemExit(main())
