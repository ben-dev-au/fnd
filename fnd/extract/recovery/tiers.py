"""Concrete extraction tiers."""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Callable
from typing import Any

from fnd.extract.recovery.evaluators import CoverageEvaluator
from fnd.extract.recovery.models import (
    FLAG_TEXTURE_RECOVERED,
    ExtractionContext,
    PageExtraction,
)

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


_HEADING_RE = re.compile(r"^#{1,6} ")


def _clamp_headings(md: str) -> str:
    """Demote every recovered heading to ``##`` so it sits below fnd's
    section breadcrumb. Fence-aware: ``#`` comment lines inside a ```` ```
    ```` code block are left untouched."""
    out: list[str] = []
    in_fence = False
    for line in md.splitlines(keepends=True):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            out.append(line)
        elif not in_fence and _HEADING_RE.match(line):
            out.append(_HEADING_RE.sub("## ", line, count=1))
        else:
            out.append(line)
    return "".join(out)


class InvisibleTextTier:
    """Recover scanned pages whose invisible OCR text the layout parser
    drops. Gate: production Markdown exists, the flat layer has enough
    tokens to judge, and production coverage sits below the floor. When
    it fires, re-extract via the ignore_alpha lever and keep whichever
    variant covers more of the flat layer."""

    def __init__(
        self,
        extract_invisible_md: ExtractPageMd,
        coverage: CoverageEvaluator,
        *,
        cov_gate: float = 0.70,
        min_flat_tokens: int = 20,
    ) -> None:
        self._extract_invisible_md = extract_invisible_md
        self._coverage = coverage
        self._cov_gate = cov_gate
        self._min_flat_tokens = min_flat_tokens

    def refine(self, ctx: ExtractionContext, current: PageExtraction) -> PageExtraction:
        old_md = current.markdown
        if not old_md:
            return current
        if self._coverage.flat_token_count(ctx.flat) < self._min_flat_tokens:
            return current
        old_cov = self._coverage.coverage(old_md, ctx.flat)
        if old_cov >= self._cov_gate:
            return dataclasses.replace(current, coverage=old_cov)
        new_md = _clamp_headings(self._extract_invisible_md(ctx.doc, ctx.page_index))
        new_cov = self._coverage.coverage(new_md, ctx.flat)
        if new_md and new_cov > old_cov:
            return PageExtraction(
                markdown=new_md,
                tier="invisible-text",
                coverage=new_cov,
                legibility=current.legibility,
                flags=current.flags | {FLAG_TEXTURE_RECOVERED},
            )
        return dataclasses.replace(current, coverage=old_cov)
