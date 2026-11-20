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
import traceback
import zipfile
from pathlib import Path

from ..core import config
from ..core.events import emit
from ..core.service import offline_session
from ..core.manifest import SyncState, finish_run, stats_text
from ..core.model import Attachment, Chat, Turn
from ..core.writer import write_chat
from . import chatgpt_references

PROVIDER = 'chatgpt'

_SKIP_CONTENT_TYPES = {'thoughts', 'reasoning_recap', 'system_message'}
_IMG_EXT = re.compile(r'\.(png|jpe?g|gif|webp)$', re.IGNORECASE)


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
    seen_nodes = {id(roots[0])}
    while True:
        children = path[-1].get('children') or []
        nxt = next((mapping[c] for c in children if c in mapping), None)
        if nxt is None or id(nxt) in seen_nodes:
            return path
        seen_nodes.add(id(nxt))
        path.append(nxt)


def _is_tool_call(msg: dict) -> bool:
    """A message addressed to a tool: a web search, code run, memory update…

    ChatGPT shows only the final reply. Official exports can omit the
    recipient; such a message is visible (chatgpt-exporter 44affd4).
    """
    recipient = msg.get('recipient')
    return recipient is not None and recipient != 'all'


def _content_parts(msg: dict) -> list:
    """Visible text/media parts, including current interactive Markdown replies.

    Citation and widget markers are resolved to what ChatGPT displays, and a
    source footnote becomes a final Sources part.
    """
    content = msg.get('content') or {}
    meta = msg.get('metadata') or {}
    sources = chatgpt_references.source_list(meta)
    if content.get('content_type') == 'text':
        fallback = (meta.get('model_dil_v2') or {}).get('fallbackMarkdown')
        if isinstance(fallback, str) and fallback.strip():
            text = re.sub(r'<escape>([\s\S]*?)</escape>', r'\1', fallback)
            return [chatgpt_references.resolve(text, meta)] + ([sources] if sources else [])
    if content.get('content_type') == 'code' and isinstance(content.get('text'), str):
        text = content['text']
        fence = '`' * max(3, max((len(m[0]) + 1 for m in re.finditer(r'`+', text)), default=3))
        language = str(content.get('language') or '').replace('\n', '')
        return [f'{fence}{language}\n{text}\n{fence}']
    parts = content.get('parts') or []
    if not isinstance(parts, list):
        return []
    parts = [chatgpt_references.resolve(p, meta) if isinstance(p, str) else p for p in parts]
    return [*parts, sources] if sources and parts else parts


def _visible_message(node: dict) -> tuple[dict, str] | None:
    """The node's message and its turn role ('user' or 'model') when ChatGPT
    shows it: not hidden, not a reasoning step or a tool call, and written by
    the user or the assistant. Shared by the live download and the export."""
    msg = node.get('message')
    if not msg:
        return None
    meta = msg.get('metadata') or {}
    if meta.get('is_visually_hidden_from_conversation') or meta.get('is_thinking_preamble_message'):
        return None
    role = (msg.get('author') or {}).get('role', '')
    if _is_tool_call(msg) or role not in ('user', 'assistant'):
        return None
    if (msg.get('content') or {}).get('content_type', '') in _SKIP_CONTENT_TYPES:
        return None
    return msg, 'user' if role == 'user' else 'model'


def _node_to_turn(node: dict) -> Turn | None:
    visible = _visible_message(node)
    if visible is None:
        return None
    msg, role = visible
    texts, images_notes = [], []
    for part in _content_parts(msg):
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
    return Turn(role=role, text=text.strip())


def _export_preambles(nodes: list[dict]) -> set[int]:
    """Positions of thinking preambles in an official data export.

    The export drops `recipient` and `is_thinking_preamble_message` but keeps
    the short progress notes a thinking model writes between reasoning
    steps. A reply without a recipient that is directly followed by a
    reasoning step is such a note (chatgpt-exporter 8e5bd2a). Live API
    messages always carry a recipient, so they are never matched."""
    found = set()
    for index, node in enumerate(nodes[:-1]):
        msg = node.get('message') or {}
        following = ((nodes[index + 1].get('message') or {}).get('content') or {})
        if (msg.get('recipient') is None and (msg.get('author') or {}).get('role') == 'assistant'
                and (msg.get('content') or {}).get('content_type') == 'text'
                and following.get('content_type') in ('thoughts', 'reasoning_recap')):
            found.add(index)
    return found


def parse_conversation(conv: dict, attachments: list[Attachment] | None = None) -> Chat | None:
    mapping = conv.get('mapping') or {}
    nodes = _linearize(mapping, conv.get('current_node'))
    preambles = _export_preambles(nodes)
    turns = [t for i, t in enumerate(_node_to_turn(n) for n in nodes) if t and i not in preambles]
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
    for i, chat in enumerate(chats, 1):
        label = chat.title or chat.id
        if sync.known(chat.id):
            emit(None, 'chat_skipped', PROVIDER, label, i, len(chats),
                 'skip (already in the database)')
            continue
        try:
            stats = write_chat(chat, out_dir)
            sync.mark(chat.id, None, str(stats['md']))
            results.append({'id': chat.id, 'title': chat.title, 'ok': True,
                            **{k: stats[k] for k in ('turns', 'chars', 'images', 'docs')}})
            emit(None, 'chat_completed', PROVIDER, label, i, len(chats), stats_text(stats))
        except Exception as e:
            results.append({'id': chat.id, 'title': label, 'ok': False, 'error': str(e)})
            emit(None, 'error', PROVIDER, label, i, len(chats), str(e), traceback.format_exc())
    sync.save()
    return results


# ---------------------------------------------------------------------------
# Source 1: official conversations.json
# ---------------------------------------------------------------------------
def _export_files(path: Path):
    """The parsed JSON files of an official export: one file, the export
    folder, or the export ZIP. A large export splits its conversations into
    conversations-000.json, conversations-001.json and so on."""
    path = Path(path)
    if path.is_dir():
        for file in sorted(path.glob('conversations*.json')):
            yield json.loads(file.read_text(encoding='utf-8'))
    elif zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as bundle:
            for name in sorted(bundle.namelist()):
                if re.fullmatch(r'(?:.*/)?conversations[^/]*\.json', name):
                    yield json.loads(bundle.read(name).decode('utf-8'))
    else:
        yield json.loads(path.read_text(encoding='utf-8'))


def collect_conversations(path: Path) -> list[dict]:
    """Every conversation in an official export. Items without a message
    tree are skipped; a conversation found twice keeps its latest version
    (chatgpt-exporter 023cf2b)."""
    found: dict[str, dict] = {}
    for data in _export_files(path):
        # some exports wrap the list in {"conversations": [...]}
        items = data.get('conversations', []) if isinstance(data, dict) else data
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict) or not isinstance(item.get('mapping'), dict):
                continue
            cid = item.get('id') or item.get('conversation_id')
            if not isinstance(cid, str):
                continue
            previous = found.get(cid)
            if previous and (previous.get('update_time') or 0) >= (item.get('update_time') or 0):
                continue
            found[cid] = {**item, 'id': cid}
    return list(found.values())


def run_official(export_path: Path, out_dir: Path) -> list[dict]:
    chats = [c for c in (parse_conversation(conv) for conv in collect_conversations(export_path))
             if c]
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
        files = sorted(f for f in files_dir.iterdir() if f.is_file()) if files_dir.is_dir() else []
        atts = [Attachment(filename=f.name, kind='image' if _IMG_EXT.search(f.name) else 'document',
                           data=f.read_bytes(), source_url='', description=f.name)
                for f in files]
        chat = parse_conversation(conv, attachments=atts)
        if chat:
            chats.append(chat)
    return _write_chats(chats, out_dir)


def main(argv=None):
    ap = argparse.ArgumentParser(prog='reclaim import chatgpt|scrapemychats')
    ap.add_argument('source', choices=['chatgpt', 'scrapemychats'])
    ap.add_argument('path', help='official export: conversations.json, the export folder or '
                                 'its ZIP (chatgpt); export dir (scrapemychats)')
    ap.add_argument('-o', '--output-dir',
                    default=str(config.archive_root() / 'ChatGPT'))
    args = ap.parse_args(argv)

    started = time.time()
    with offline_session(Path(args.output_dir).parent, f'import {args.source}'):
        if args.source == 'chatgpt':
            results = run_official(Path(args.path), Path(args.output_dir))
        else:
            results = run_scrapemychats(Path(args.path), Path(args.output_dir))
        return finish_run(Path(args.output_dir), PROVIDER, results, started)


if __name__ == '__main__':
    raise SystemExit(main())
