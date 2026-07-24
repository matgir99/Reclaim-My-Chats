"""Canonical data model for ReclaimMyChats.

Every provider mode (scrape/parse/import) produces a `Chat` with fully
materialized content (image/attachment bytes in memory). `core.writer` is the
only place that writes files.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Attachment:
    """A file referenced by a turn (user upload or model-produced document)."""
    filename: str                 # sanitized; de-duplicated by writer
    kind: str = 'document'        # 'document' | 'image'
    data: bytes | None = None     # bytes to save; None -> link-only
    source_url: str = ''          # fallback link when data is None
    description: str = ''


@dataclass
class Turn:
    """One conversation turn (thoughts are kept here, filtered at write time)."""
    role: str                     # 'user' | 'model'
    text: str = ''
    thought: bool = False
    images: list[bytes] = field(default_factory=list)   # inline images
    attachments: list[Attachment] = field(default_factory=list)
    error: str | None = None      # API-side error marker, if any


@dataclass
class Chat:
    id: str
    title: str
    source_url: str
    turns: list[Turn] = field(default_factory=list)
    provider: str = ''

    @property
    def had_thoughts(self) -> bool:
        return any(t.thought for t in self.turns)

    @property
    def errors(self) -> list[str]:
        return [t.error for t in self.turns if t.error]

    def visible_turns(self) -> list[Turn]:
        """Turns that belong in the archive (thoughts excluded)."""
        return [t for t in self.turns if not t.thought]
