"""Concrete extraction tiers."""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Callable
from typing import Any

from fnd.extract.recovery.evaluators import CoverageEvaluator, LegibilityEvaluator
from fnd.extract.recovery.models import (
    FLAG_DOCLING_INVOKED,
    FLAG_LOW_QUALITY,
    FLAG_TEXTURE_RECOVERED,
    ExtractionContext,
    PageExtraction,
)

# Injected from pdf.py so the recovery package stays free of pymupdf4llm
# specifics: (doc, page_index) -> page Markdown. ``doc`` is a
# pymupdf.Document, typed Any here to keep the package import-light.
ExtractPageMd = Callable[[Any, int], str]

# A captioned table on a scanned page: "Table 5.1:" / "Table 5:". The
# colon keeps the match specific (0 false positives across the measured
# 417-page corpus); recovered scanned tables flatten to prose, so this
# flat-text caption is the only reliable detector (no picture markers).
TABLE_CAPTION_RE = re.compile(r"\bTable\s+\d+(?:\.\d+)?\s*:")


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


# Injected docling primitives, kept in pdf.py: (path, page_index) -> page
# Markdown, and Markdown -> list of contiguous `| ... |` table blocks.
DoclingExtract = Callable[[str, int], str]
ExtractTables = Callable[[str], list[str]]


def _insert_tables_at_captions(prose: str, tables: list[str], caption_re: re.Pattern[str]) -> str:
    """Drop each recovered grid in just after its caption line, keeping
    the surrounding prose. Tables with no matching caption (or once the
    captions run out) are appended, so a recovered grid is never lost."""
    lines = prose.splitlines()
    remaining = list(tables)
    out: list[str] = []
    for line in lines:
        out.append(line)
        if remaining and caption_re.search(line):
            out.append("")
            out.append(remaining.pop(0))
    result = "\n".join(out)
    if remaining:
        result = result.rstrip("\n") + "\n\n" + "\n\n".join(remaining)
    return result + "\n" if prose.endswith("\n") else result


class DoclingTableTier:
    """Recover the grid of a captioned table on a scanned page.

    The invisible-text lever recovers a table's text but flattens it to
    prose (the grid is gone and it emits no picture marker, so the
    born-digital splice can't fire). When a recovered page carries a
    table caption, route it through docling — whose layout model rebuilds
    the grid — and splice the grid in at the caption, preserving the
    recovered prose. Only acts on texture-recovered pages; born-digital
    pages keep the existing marker-based inline splice."""

    def __init__(
        self,
        docling_extract: DoclingExtract,
        extract_tables: ExtractTables,
        *,
        caption_re: re.Pattern[str] = TABLE_CAPTION_RE,
    ) -> None:
        self._docling_extract = docling_extract
        self._extract_tables = extract_tables
        self._caption_re = caption_re

    def refine(self, ctx: ExtractionContext, current: PageExtraction) -> PageExtraction:
        if FLAG_TEXTURE_RECOVERED not in current.flags:
            return current
        if not self._caption_re.search(ctx.flat):
            return current
        docling_md = self._docling_extract(ctx.path, ctx.page_index)
        tables = self._extract_tables(docling_md) if docling_md else []
        if not tables:
            return current  # genuine figure, or docling recovered no grid
        spliced = _insert_tables_at_captions(current.markdown, tables, self._caption_re)
        return dataclasses.replace(
            current,
            markdown=spliced,
            tier="docling-table",
            flags=current.flags | {FLAG_DOCLING_INVOKED},
        )


# Injected reprocessors for the (deferred) legibility tier: native OCR of
# a page, and full-page docling extraction.
NativeOcr = Callable[[Any], str]


class LegibilityReprocessTier:
    """Reprocess a page whose recovered prose reads as garbled OCR.

    DEFERRED — built and unit-tested but not composed into the pipeline:
    the measured corpus has uniformly legible baked OCR, so no real page
    exercises it. Kept ready for a poorer-scan corpus. When prose
    legibility falls below the gate (with enough prose to judge), it tries
    native pymupdf OCR, then full-page docling, keeping the first variant
    that lifts legibility by a margin; otherwise it flags the page
    low-quality and leaves the text untouched."""

    def __init__(
        self,
        legibility: LegibilityEvaluator,
        native_ocr: NativeOcr,
        docling_extract: DoclingExtract,
        *,
        legr_gate: float = 0.80,
        min_tokens: int = 30,
        min_gain: float = 0.03,
    ) -> None:
        self._legibility = legibility
        self._native_ocr = native_ocr
        self._docling_extract = docling_extract
        self._legr_gate = legr_gate
        self._min_tokens = min_tokens
        self._min_gain = min_gain

    def refine(self, ctx: ExtractionContext, current: PageExtraction) -> PageExtraction:
        legr, tokens = self._legibility.prose_legr(current.markdown)
        if legr is None or tokens < self._min_tokens or legr >= self._legr_gate:
            return current

        ocr_md = self._native_ocr(ctx.page)
        if self._gain(ocr_md, legr):
            ocr_legr, _ = self._legibility.prose_legr(ocr_md)
            return dataclasses.replace(
                current, markdown=ocr_md, tier="legibility-ocr", legibility=ocr_legr
            )

        docling_md = self._docling_extract(ctx.path, ctx.page_index)
        if self._gain(docling_md, legr):
            doc_legr, _ = self._legibility.prose_legr(docling_md)
            return dataclasses.replace(
                current,
                markdown=docling_md,
                tier="legibility-docling",
                legibility=doc_legr,
                flags=current.flags | {FLAG_DOCLING_INVOKED},
            )

        return dataclasses.replace(
            current, legibility=legr, flags=current.flags | {FLAG_LOW_QUALITY}
        )

    def _gain(self, candidate: str, baseline: float) -> bool:
        if not candidate:
            return False
        legr, _ = self._legibility.prose_legr(candidate)
        return legr is not None and legr - baseline >= self._min_gain
