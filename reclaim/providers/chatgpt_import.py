"""ChatGPT importers (import mode).

Two sources:

1. Official export (``conversations.json`` from chatgpt.com → Settings →
   Data Controls → Export). Complete text history as message trees;
   media/files are NOT included (asset pointers are authenticated
   ``file-service://`` URLs that don't work offline) — image parts become
   placeholder notes.

2. scrapemychats export dirs (https://github.com/conradqh/scrapemychats,
   MIT): per-chat folders with ``conversation.json`` (same mapping
   structure) plus a ``files/`` dir with downloaded attachments. Files are
   attached to the chat (name-matched to turns when possible, else listed
   on the final turn).

Message-tree linearization: follow the path from the root to
``current_node`` (the branch ChatGPT displays). Hidden/system/tool messages
are skipped; reasoning/thoughts blocks are excluded per project policy.
"""

from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path

from ..core.manifest import SyncState, write_manifest
from ..core.model import Attachment, Chat, Turn
from ..core.writer import write_chat

PROVIDER = 'chatgpt'

_SKIP_CONTENT_TYPES = {'thoughts', 'reasoning_recap', 'system_message'}
_IMG_EXT = re.compile(r'\.(png|jpe?g|gif|webp)$', re.I)


def _linearize(mapping: dict, current_node: str | None) -> list[dict]:
    """Return the list of nodes on the root->current_node path."""
    if not mapping:
        return []
    # child -> parent is given by node['parent']; walk up from current_node
    if current_node and current_node in mapping:
        path, nid, seen = [], current_node, set()
        while nid and nid in mapping and nid not in seen:
            seen.add(nid)
            path.append(mapping[nid])
            nid = mapping[nid].get('parent')
        return list(reversed(path))
    # fallback: root -> always last child (most recent branch)
    roots = [n for n in mapping.values() if not n.get('parent')]
    if not roots:
        return []
    path = [roots[0]]
    while True:
        children = path[-1].get('children') or []
        nxt = next((mapping[c] for c in children if c in mapping), None)
        if nxt is None:
            return path
        path.append(nxt)


def _node_to_turn(node: dict) -> Turn | None:
    msg = node.get('message')
    if not msg:
        return None
    meta = msg.get('metadata') or {}
    if meta.get('is_visually_hidden_from_conversation'):
        return None
    role = (msg.get('author') or {}).get('role', '')
    if role not in ('user', 'assistant'):
        return None
    content = msg.get('content') or {}
    ctype = content.get('content_type', '')
    if ctype in _SKIP_CONTENT_TYPES:
        return None

    texts, images_notes = [], []
    for part in content.get('parts', []) or []:
        if isinstance(part, str):
            texts.append(part)
        elif isinstance(part, dict):
            # multimodal parts: image/file asset pointers (not downloadable
            # from the official export)
            inner = part.get('content_type', '')
            if 'image' in inner or 'asset_pointer' in part:
                images_notes.append('[image: not included in official export]')
            elif part.get('text'):
                texts.append(part['text'])

    text = '\n\n'.join(t for t in texts if t and t.strip())
    if images_notes:
        text = (text + '\n\n' if text else '') + '\n\n'.join(images_notes)
    if not text.strip():
        return None
    return Turn(role='user' if role == 'user' else 'model', text=text.strip())


def parse_conversation(conv: dict, attachments: list[Attachment] | None = None) -> Chat | None:
    mapping = conv.get('mapping') or {}
    nodes = _linearize(mapping, conv.get('current_node'))
    turns = [t for t in (_node_to_turn(n) for n in nodes) if t]
    if not turns:
        return None
    if attachments:
        turns[-1].attachments.extend(attachments)
    return Chat(id=conv.get('id', ''), title=conv.get('title', '') or '(untitled)',
                source_url=f"https://chatgpt.com/c/{conv.get('id', '')}",
                turns=turns, provider=PROVIDER)


def _write_chats(chats: list[Chat], out_dir: Path) -> list[dict]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    sync = SyncState(out_dir, PROVIDER)
    results = []
    for chat in chats:
        label = (chat.title or chat.id)[:55]
        if sync.known(chat.id):
            print(f'[skip] {label}')
            continue
        try:
            stats = write_chat(chat, out_dir)
            sync.mark(chat.id, None, str(stats['md']))
            results.append({'id': chat.id, 'title': chat.title, 'ok': True,
                            **{k: stats[k] for k in ('turns', 'chars', 'images', 'docs')}})
            print(f'[ok] {label} -> {stats["turns"]}t, {stats["chars"]:,} chars')
        except Exception as e:
            results.append({'id': chat.id, 'title': label, 'ok': False, 'error': str(e)})
            print(f'[FAIL] {label}: {e}')
    sync.save()
    return results


# ---------------------------------------------------------------------------
# Source 1: official conversations.json
# ---------------------------------------------------------------------------
def run_official(export_path: Path, out_dir: Path) -> list[dict]:
    data = json.loads(Path(export_path).read_text(encoding='utf-8'))
    if isinstance(data, dict):  # some exports wrap in {"conversations": [...]}
        data = data.get('conversations', [])
    chats = [c for c in (parse_conversation(conv) for conv in data) if c]
    return _write_chats(chats, out_dir)


# ---------------------------------------------------------------------------
# Source 2: scrapemychats export dirs (with files/)
# ---------------------------------------------------------------------------
def run_scrapemychats(export_dir: Path, out_dir: Path) -> list[dict]:
    chats = []
    for chat_dir in sorted(Path(export_dir).iterdir()):
        if not chat_dir.is_dir():
            continue
        conv_file = chat_dir / 'conversation.json'
        if not conv_file.exists():
            continue
        try:
            conv = json.loads(conv_file.read_text(encoding='utf-8'))
        except Exception:
            continue
        files_dir = chat_dir / 'files'
        atts = []
        if files_dir.is_dir():
            for f in sorted(files_dir.iterdir()):
                if f.is_file():
                    atts.append(Attachment(
                        filename=f.name,
                        kind='image' if _IMG_EXT.search(f.name) else 'document',
                        data=f.read_bytes(), source_url='', description=f.name))
        chat = parse_conversation(conv, attachments=atts)
        if chat:
            chats.append(chat)
    return _write_chats(chats, out_dir)


def main(argv=None):
    ap = argparse.ArgumentParser(prog='reclaim import chatgpt|scrapemychats')
    ap.add_argument('source', choices=['chatgpt', 'scrapemychats'])
    ap.add_argument('path', help='conversations.json (chatgpt) or export dir (scrapemychats)')
    ap.add_argument('-o', '--output-dir',
                    default=str(Path(__file__).resolve().parents[2] / 'ChatGPT'))
    args = ap.parse_args(argv)

    started = time.time()
    if args.source == 'chatgpt':
        results = run_official(Path(args.path), Path(args.output_dir))
    else:
        results = run_scrapemychats(Path(args.path), Path(args.output_dir))
    manifest = write_manifest(Path(args.output_dir), PROVIDER, results, started)
    ok = sum(1 for r in results if r.get('ok'))
    print(f"\nDone: {ok}/{len(results)} ok. Manifest: {manifest}")
    return 0 if ok == len(results) else 2


if __name__ == '__main__':
    raise SystemExit(main())
