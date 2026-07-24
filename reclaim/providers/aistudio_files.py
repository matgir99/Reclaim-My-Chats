"""Google AI Studio provider (parse mode) — offline Drive/Takeout files.

AI Studio stores every prompt as a JSON file (MIME type
``application/vnd.google-makersuite.prompt``) in a visible "Google AI Studio"
Google Drive folder; Google Takeout exports the same files. Those files are
self-describing (named fields), unlike the positional RPC response:

  chunkedPrompt.chunks[]: {text, role, tokenCount, createTime,
                           isThought (bool), parts[], finishReason, grounding}
  image parts:            {inlineData: {mimeType, data(base64)}}
  attachments:            {fileData: {fileId/fileUri, ...}} (user uploads)

This module parses such files offline into the canonical Chat. Titles are not
stored in the file (they live in Drive metadata) — pass a titles map
(drive_id -> title) when available, else the file stem is used.
"""

from __future__ import annotations

import argparse
import base64
import json
import zipfile
from pathlib import Path

from ..core.manifest import write_manifest
from ..core.model import Attachment, Chat, Turn
from ..core.writer import write_chat
import time

PROVIDER = 'aistudio'
_IMG_MIMES = ('image/png', 'image/jpeg', 'image/gif', 'image/webp')


def parse_prompt_file(data: dict, chat_id: str = '', title: str = '') -> Chat | None:
    """Parse one makersuite.prompt JSON into a canonical Chat."""
    if not isinstance(data, dict) or 'chunkedPrompt' not in data:
        return None
    chunks = data.get('chunkedPrompt', {}).get('chunks', [])
    turns = []
    for ch in chunks:
        if not isinstance(ch, dict):
            continue
        role = ch.get('role', '')
        if role not in ('user', 'model'):
            continue
        text = ch.get('text', '') or ''
        thought = bool(ch.get('isThought'))
        images, atts = [], []

        for part in ch.get('parts', []) or []:
            if not isinstance(part, dict):
                continue
            inline = part.get('inlineData') or part.get('inline_data') or {}
            if isinstance(inline, dict) and inline.get('data'):
                try:
                    images.append(base64.b64decode(inline['data']))
                except Exception:
                    pass
            fdata = part.get('fileData') or part.get('file_data') or {}
            if isinstance(fdata, dict):
                fid = fdata.get('fileId') or fdata.get('file_id') or ''
                uri = fdata.get('fileUri') or fdata.get('file_uri') or ''
                name = fdata.get('displayName') or fdata.get('name') or f'drive_file_{fid[:12]}'
                if fid or uri:
                    atts.append(Attachment(
                        filename=name,
                        kind='document',
                        data=None,
                        source_url=uri or f'https://drive.google.com/file/d/{fid}/view',
                        description=name,
                    ))

        if not text.strip() and not images and not atts:
            continue
        turns.append(Turn(role=role, text=text, thought=thought,
                          images=images, attachments=atts))

    if not turns:
        return None
    return Chat(id=chat_id, title=title or chat_id, source_url='', turns=turns,
                provider=PROVIDER)


def _iter_prompt_files(folder: Path):
    """Yield (name, json-dict) for every makersuite.prompt file in folder
    (or a Takeout/Drive .zip)."""
    folder = Path(folder)
    if folder.is_file() and folder.suffix.lower() == '.zip':
        with zipfile.ZipFile(folder) as z:
            for info in z.infolist():
                if info.is_dir():
                    continue
                try:
                    data = json.loads(z.read(info.filename))
                except Exception:
                    continue
                yield Path(info.filename).stem, data
        return
    for p in sorted(folder.rglob('*')):
        if not p.is_file() or p.suffix.lower() in ('.md', '.txt', '.csv', '.html'):
            continue
        try:
            data = json.loads(p.read_text(encoding='utf-8'))
        except Exception:
            continue
        yield p.stem, data


def run(folder: Path, out_dir: Path, titles: dict | None = None,
        source_url_tpl: str = 'https://aistudio.google.com/prompts/{id}') -> list[dict]:
    titles = titles or {}
    results = []
    for stem, data in _iter_prompt_files(folder):
        chat = parse_prompt_file(data, chat_id=stem, title=titles.get(stem, stem))
        if chat is None:
            continue
        chat.source_url = source_url_tpl.format(id=stem) if stem else ''
        try:
            stats = write_chat(chat, out_dir)
            results.append({'id': stem, 'title': chat.title, 'ok': True,
                            **{k: stats[k] for k in ('turns', 'chars', 'images', 'docs')}})
            print(f'[ok] {chat.title[:60]} -> {stats["turns"]}t, '
                  f'{stats["chars"]:,} chars, {stats["images"]} img, {stats["docs"]} doc')
        except Exception as e:
            results.append({'id': stem, 'title': chat.title, 'ok': False, 'error': str(e)})
            print(f'[FAIL] {chat.title[:60]}: {e}')
    return results


def main(argv=None):
    ap = argparse.ArgumentParser(prog='reclaim parse aistudio')
    ap.add_argument('--from-folder', required=True,
                    help='Folder of Drive-exported prompt JSONs (or a Takeout .zip)')
    ap.add_argument('--titles', help='Optional JSON map: drive_id -> title')
    ap.add_argument('-o', '--output-dir',
                    default=str(Path(__file__).resolve().parents[2] / 'Google AI Studio'))
    args = ap.parse_args(argv)
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    titles = json.loads(Path(args.titles).read_text()) if args.titles else {}

    started = time.time()
    results = run(Path(args.from_folder), out_dir, titles=titles)
    manifest = write_manifest(out_dir, PROVIDER + '-offline', results, started)
    ok = sum(1 for r in results if r.get('ok'))
    print(f"\nDone: {ok}/{len(results)} ok. Manifest: {manifest}")
    return 0 if ok == len(results) else 2


if __name__ == '__main__':
    raise SystemExit(main())
