"""ChatGPT content references: citation and widget markers in reply text.

ChatGPT writes private-use markers into replies, for example
``\\ue200cite\\ue202turn0search3\\ue201``, and describes each one in
``metadata.content_references``. The web app draws them as citation pills,
links, names or widgets. Left alone they reach the archive as unreadable
text such as ``citeturn0search3``.

Each referenced marker becomes what ChatGPT shows: web citations become
links, file citations the file name, entities their name, and hidden or
interactive-only markers nothing. A message's source footnote becomes a
**Sources** list. Unreferenced markers are removed whole, except inline
links and entity names, which carry their own text.

Adapted from pionxzh/chatgpt-exporter ``src/utils/citations.ts`` (MIT,
revision 44affd4). Unlike upstream, ordinary text is not normalized, so
dashes and non-breaking spaces in replies are kept as written.
"""

from __future__ import annotations

import json
import re

_MARKER = re.compile(r'\ue200([a-z_]+)((?:\ue202[^\ue200\ue201]*)*)\ue201')
_PRIVATE_USE = re.compile(r'[\ue200-\ue20f]')
_EMPTY_LINK = re.compile(r'\[([^\]]*)\]\(\)')
_HTTP = re.compile(r'https?://', re.I)


def _dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def _list(value) -> list:
    return value if isinstance(value, list) else []


def _str(value) -> str:
    return value if isinstance(value, str) else ''


def _escape_text(text: str) -> str:
    return (text.replace('\\', '\\\\').replace('[', '\\[').replace(']', '\\]')
            .replace('\n', ' '))


def _escape_url(url: str) -> str:
    return url.replace('<', '%3C').replace('>', '%3E').replace('\n', '')


def _http_url(*candidates) -> str:
    for value in candidates:
        if isinstance(value, str) and _HTTP.match(value.strip()):
            return value.strip()
    return ''


def _link(source: dict) -> str:
    label = next((source[k].strip() for k in ('attribution', 'title', 'url')
                  if isinstance(source.get(k), str) and source[k].strip()), 'Source')
    url = _http_url(source.get('url'))
    if not url:
        return _escape_text(label)
    return f'[{_escape_text(label)}](<{_escape_url(url)}>)'


def _dedupe(sources: list[dict]) -> list[dict]:
    seen, result = set(), []
    for source in sources:
        key = _http_url(source.get('url')) or _link(source)
        if key not in seen:
            seen.add(key)
            result.append(source)
    return result


def _alt(ref: dict) -> str:
    alt = ref.get('alt')
    # Product references link to nothing, e.g. `[Product name]()`.
    return _EMPTY_LINK.sub(r'\1', alt) if isinstance(alt, str) else ''


def _marker_fields(marker: str) -> list[str]:
    match = _MARKER.fullmatch(marker)
    return match.group(2).split('\ue202')[1:] if match else []


def _entity_name(data: str) -> str | None:
    try:
        parsed = json.loads(data)
    except ValueError:
        return None
    if isinstance(parsed, list) and len(parsed) > 1 and isinstance(parsed[1], str):
        return parsed[1]
    return None


def _images(ref: dict) -> str:
    links = []
    for entry in _list(ref.get('images')):
        if not isinstance(entry, dict):
            continue
        image = _dict(entry.get('image_result')) or entry
        src = _http_url(image.get('content_url'))
        if not src:
            continue
        title = _str(image.get('title'))
        markdown = f'![{_escape_text(title.strip() or "Image")}](<{_escape_url(src)}>)'
        page = _http_url(image.get('url'))
        links.append(f'[{markdown}](<{_escape_url(page)}>)' if page else markdown)
    return '\n\n'.join(links)


def _chart(chart) -> str:
    """A chart widget becomes its description and a table of its data."""
    if not isinstance(chart, dict):
        return ''
    x_key, series, data = chart.get('xKey'), chart.get('series'), chart.get('data')
    if not x_key or not isinstance(series, list) or not isinstance(data, list) or not data:
        return ''
    series = [s for s in series if isinstance(s, dict) and s.get('dataKey')]
    if not series:
        return ''
    formatted = _list(chart.get('formattedData'))

    def row(cells) -> str:
        return '| ' + ' | '.join(str('' if c is None else c).replace('|', '\\|')
                                 .replace('\n', ' ') for c in cells) + ' |'

    rows = [row([x_key] + [s.get('label') or s['dataKey'] for s in series]),
            row(['---'] * (len(series) + 1))]
    for index, point in enumerate(data):
        if not isinstance(point, dict):
            continue
        shown = _dict(formatted[index]) if index < len(formatted) else {}
        cells = [point.get(x_key)]
        for s in series:
            value = shown.get(s['dataKey'])
            if value is None and point.get(s['dataKey']) is not None:
                value = f"{point[s['dataKey']]}{s.get('valueSuffix') or ''}"
            cells.append(value)
        rows.append(row(cells))
    table = '\n'.join(rows)
    description = _str(_dict(chart.get('meta')).get('description'))
    return f'{description.strip()}\n\n{table}' if description.strip() else table


def _inline(ref: dict) -> str:
    kind = ref.get('type')
    if kind == 'hidden':
        return ''
    if kind == 'client_defined_widget':
        data = _dict(ref.get('data'))
        if data.get('widget_type') == 'charts_widget_v2':
            return _chart(data.get('content'))
    if kind == 'url':
        fields = _marker_fields(ref.get('matched_text') or '')
        label = _str(ref.get('title')) or (fields[0] if fields else '')
        url = _http_url(fields[1] if len(fields) > 1 else None,
                        *_list(ref.get('safe_urls')), _dict(ref.get('item')).get('url'))
        return _link({'title': label, 'url': url}) if label or url else ''
    if kind == 'entity':
        name = ref.get('name')
        if not isinstance(name, str):
            fields = _marker_fields(ref.get('matched_text') or '')
            name = _entity_name(fields[0]) if fields else None
        return _escape_text(name) if name else _alt(ref)
    if kind in ('image_group', 'image_v2'):
        return _images(ref) or _alt(ref)

    sources: list[dict] = []
    for item in ref.get('items') or []:
        if isinstance(item, dict):
            sources.append(item)
            sources.extend(s for s in item.get('supporting_websites') or [] if isinstance(s, dict))
    sources.extend(s for s in ref.get('fallback_items') or [] if isinstance(s, dict))
    if not sources and kind == 'file' and isinstance(ref.get('name'), str):
        sources.append({'title': ref['name']})
    if not sources and any(isinstance(ref.get(k), str) and ref[k].strip()
                           for k in ('url', 'title', 'attribution')):
        sources.append(ref)
    if not sources:
        sources = [{'title': u, 'url': u} for u in ref.get('safe_urls') or [] if _http_url(u)]
    sources = _dedupe(sources)
    if sources:
        return '(' + ', '.join(_link(s) for s in sources) + ')'
    return _alt(ref)


def _unreferenced(match: re.Match) -> str:
    kind, fields = match.group(1), match.group(2).split('\ue202')[1:]
    if kind == 'url' and fields:
        url = _http_url(fields[1] if len(fields) > 1 else None)
        return _link({'title': fields[0], 'url': url}) if fields[0] or url else ''
    if kind == 'entity' and fields:
        name = _entity_name(fields[0])
        return _escape_text(name) if name else ''
    return ''


def _references(metadata) -> list[dict]:
    refs = metadata.get('content_references') if isinstance(metadata, dict) else None
    return [r for r in refs if isinstance(r, dict)] if isinstance(refs, list) else []


def resolve(text: str, metadata) -> str:
    """Replace every marker in one text part with what ChatGPT displays."""
    if not isinstance(text, str):
        return text
    refs = [r for r in _references(metadata)
            if r.get('type') != 'sources_footnote'
            and isinstance(r.get('matched_text'), str) and r['matched_text'].strip()]
    files = set()
    for ref in sorted(refs, key=lambda r: len(r['matched_text']), reverse=True):
        data = _dict(ref.get('data'))
        # The text already contains the Markdown of a code-block widget.
        if ref.get('type') == 'client_defined_widget' and data.get('widget_type') == 'code_block':
            continue
        if ref['matched_text'] not in text:
            continue
        replacement = _inline(ref)
        text = text.replace(ref['matched_text'], replacement)
        if ref.get('type') == 'file' and replacement:
            files.add(replacement)
    # Citations of several passages in one file would repeat its name.
    for name in files:
        escaped = re.escape(name)
        text = re.sub(f'{escaped}(?:\\s*{escaped})+', lambda _: name, text)
    text = _MARKER.sub(_unreferenced, text)
    return _PRIVATE_USE.sub('', text)


def source_list(metadata) -> str:
    """The message's source footnote as a Markdown list, or ''."""
    sources: list[dict] = []
    for ref in _references(metadata):
        if ref.get('type') != 'sources_footnote':
            continue
        for key in ('sources', 'items', 'fallback_items'):
            found = [s for s in ref.get(key) or [] if isinstance(s, dict)]
            if found:
                sources.extend(found)
                break
        else:
            sources.extend({'title': u, 'url': u} for u in ref.get('safe_urls') or []
                           if _http_url(u))
    sources = _dedupe(sources)
    if not sources:
        return ''
    return '**Sources:**\n\n' + '\n'.join(f'- {_link(s)}' for s in sources)
