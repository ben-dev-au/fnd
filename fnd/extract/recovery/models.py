"""Value objects passed through the page-recovery pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Provenance/status flags collected per page (texture-status UI deferred).
FLAG_DOCLING_INVOKED = "docling-invoked"
FLAG_LOW_QUALITY = "low-quality"
FLAG_FIGURE_INCOMPLETE = "figure-incomplete"
FLAG_TEXTURE_RECOVERED = "texture-recovered"


@dataclass(frozen=True)
class PageExtraction:
    """One page's structured Markdown plus the provenance of how it was
    produced. ``tier`` records the last tier that changed ``markdown``;
    ``coverage``/``legibility`` cache the quality scores so downstream
    tiers don't recompute them."""

    markdown: str
    tier: str = "none"
    coverage: float | None = None
    legibility: float | None = None
    flags: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True, eq=False)
class ExtractionContext:
    """Everything the tiers need about the page under extraction. ``flat``
    is ``page.get_text("text")`` — already in scope at the call site, so
    the coverage gate is free."""

    doc: Any  # pymupdf.Document
    page: Any  # pymupdf.Page
    page_index: int
    path: str
    flat: str
