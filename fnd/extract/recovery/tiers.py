"""Concrete extraction tiers."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fnd.extract.recovery.models import ExtractionContext, PageExtraction

# Injected from pdf.py so the recovery package stays free of pymupdf4llm
# specifics: (doc, page_index) -> page Markdown. ``doc`` is a
# pymupdf.Document, typed Any here to keep the package import-light.
ExtractPageMd = Callable[[Any, int], str]


class ProductionLayoutTier:
    """Baseline: today's pymupdf4llm layout-mode extraction, verbatim."""

    def __init__(self, extract_page_md: ExtractPageMd) -> None:
        self._extract_page_md = extract_page_md

    def refine(self, ctx: ExtractionContext, current: PageExtraction) -> PageExtraction:
        md = self._extract_page_md(ctx.doc, ctx.page_index)
        return PageExtraction(markdown=md, tier="production-layout")
