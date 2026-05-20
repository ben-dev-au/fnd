"""Common types for extractors.

A :class:`Chunk` is one indexed unit (one page of a PDF, one slide of a PPTX, one
heading-section of a DOCX/MD, one fixed window of a TXT). Every chunk shares a
``parent_id`` so the query layer can group hits back into per-file results.

:class:`ExtractError` is the single failure type that propagates out of
``extract``. It wraps both *rejections* (we refused to parse — encrypted,
decompression-bomb threshold tripped) and *crashes* (the parser raised
something unhelpful). The indexer catches it and continues to the next
file, so a single hostile or corrupt document can't deny indexing of
its entire collection.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Kind = Literal["pdf", "pptx", "docx", "md", "txt"]


class ExtractError(Exception):
    """Raised when a file can't be safely extracted.

    ``path`` is the offending file; ``reason`` is a short human-readable
    explanation suitable for printing to stderr.
    """

    def __init__(self, path: str, reason: str) -> None:
        super().__init__(f"{path}: {reason}")
        self.path = path
        self.reason = reason


@dataclass(slots=True, frozen=True)
class Block:
    """One renderable element of body_struct (preview pane).

    ``kind`` is one of "h1".."h6", "p", "ul", "ol", "code", "quote".
    """

    kind: str
    text: str


@dataclass(slots=True)
class Chunk:
    parent_id: str
    path: str
    mtime: int
    kind: Kind
    body: str
    body_struct: list[Block] = field(default_factory=list)
    # Original source for the structural preview renderer. For markdown
    # this is the verbatim section source; for docx/pptx it is the
    # serialised-to-markdown content (set by the relevant extractor).
    # Empty string for formats without a structural renderer (pdf, txt).
    body_md: str = ""
    # ``page`` is the *PDF page index* (1-based, sequential from cover) —
    # used by the opener to deep-link via Skim. ``page_label`` is the
    # *printed* page label as set in the PDF (e.g. "292" or "iv"); empty
    # when the PDF has no labels. The two diverge for any book with
    # roman-numbered front matter, where printed page 1 might be PDF
    # page 39. Display layers prefer ``page_label`` when present.
    page: int = 0  # 1-based; 0 = not applicable
    page_label: str = ""
    slide: int = 0  # 1-based; 0 = not applicable
    # 1-based source-line index of the chunk's first character. MD
    # extractor sets it to the heading_open line; TXT extractor counts
    # newlines before the chunk start. 0 for kinds without line tracking
    # (PDF / DOCX / PPTX). Reaches the opener via Hit.line for the
    # ``code -g {path}:{line}:1`` family of templates.
    line: int = 0
    heading_path: str = ""
    title: str = ""
    author: str = ""
    chunk_seq: int = 0
