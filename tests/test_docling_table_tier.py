"""DoclingTableTier — recover scanned-table grids.

A recovered scanned page flattens its table to prose and emits no
picture marker, so the born-digital splice can't fire. This tier detects
the table by its flat-text caption, routes the page through docling, and
splices the recovered grid in at the caption while keeping the prose.

Docling itself is faked here (real grid recovery is validated on the
corpus); the table parsing reuses the production ``_extract_md_tables``.
"""

from __future__ import annotations

import pytest

from fnd.extract.pdf import _extract_md_tables
from fnd.extract.recovery import (
    FLAG_DOCLING_INVOKED,
    FLAG_TEXTURE_RECOVERED,
    TABLE_CAPTION_RE,
    DoclingTableTier,
    ExtractionContext,
    PageExtraction,
)
from fnd.extract.recovery.tiers import _insert_tables_at_captions


def _ctx(flat: str, path: str = "x.pdf", page_index: int = 4) -> ExtractionContext:
    return ExtractionContext(
        doc=object(), page=object(), page_index=page_index, path=path, flat=flat
    )


def _recovered(markdown: str) -> PageExtraction:
    return PageExtraction(
        markdown=markdown, tier="invisible-text", flags=frozenset({FLAG_TEXTURE_RECOVERED})
    )


# ── caption regex ────────────────────────────────────────────────────────────
@pytest.mark.parametrize("text", ["Table 5.1: Patterns", "Table 12: Summary", "see Table 3.4:  x"])
def test_caption_regex_matches_numbered_captions(text: str) -> None:
    assert TABLE_CAPTION_RE.search(text)


@pytest.mark.parametrize(
    "text", ["Table 5 patterns", "Tables: many", "a stable surface", "Table: x"]
)
def test_caption_regex_rejects_non_captions(text: str) -> None:
    assert TABLE_CAPTION_RE.search(text) is None


# ── _insert_tables_at_captions ───────────────────────────────────────────────
def test_insert_drops_grid_after_caption_and_keeps_prose() -> None:
    prose = "intro prose\n\nTable 1: Sizes\n\nflattened a b c\n\ntrailing prose\n"
    table = "| a | b |\n|---|---|\n| 1 | 2 |"
    out = _insert_tables_at_captions(prose, [table], TABLE_CAPTION_RE)
    assert "intro prose" in out
    assert "trailing prose" in out
    # grid sits right after its caption line
    lines = out.splitlines()
    cap = next(i for i, ln in enumerate(lines) if ln.startswith("Table 1:"))
    assert "| a | b |" in lines[cap + 2]


def test_insert_appends_when_no_caption_match() -> None:
    prose = "just prose, no caption here\n"
    table = "| a |\n|---|"
    out = _insert_tables_at_captions(prose, [table], TABLE_CAPTION_RE)
    assert out.startswith("just prose")
    assert out.rstrip().endswith("|---|")


# ── DoclingTableTier gate ────────────────────────────────────────────────────
def _tier(docling_md: str, calls: list[tuple[str, int]] | None = None) -> DoclingTableTier:
    def fake_docling(path: str, page_index: int) -> str:
        if calls is not None:
            calls.append((path, page_index))
        return docling_md

    return DoclingTableTier(fake_docling, _extract_md_tables)


def test_tier_skips_born_digital_pages() -> None:
    """No texture-recovered flag → leave the page for the inline splice."""
    calls: list[tuple[str, int]] = []
    born = PageExtraction(markdown="Table 1: x\n\nbody", tier="production-layout")
    out = _tier("| a |\n|---|", calls).refine(_ctx("Table 1: x"), born)
    assert out is born
    assert calls == []  # docling never invoked


def test_tier_skips_when_no_caption() -> None:
    calls: list[tuple[str, int]] = []
    out = _tier("| a |\n|---|", calls).refine(_ctx("no caption here at all"), _recovered("prose"))
    assert out.tier == "invisible-text"
    assert calls == []


def test_tier_passthrough_when_docling_finds_no_table() -> None:
    """A captioned figure (docling returns no grid) keeps the recovered prose."""
    page = _recovered("Table 2: Flow\n\nflattened content")
    out = _tier("just prose, no grid").refine(_ctx("Table 2: Flow"), page)
    assert out.markdown == page.markdown
    assert FLAG_DOCLING_INVOKED not in out.flags


def test_tier_splices_grid_and_flags() -> None:
    page = _recovered("Table 3: Costs\n\nflattened a b c d\n\ntrailing")
    docling_md = "## ignored heading\n\n| metric | value |\n|---|---|\n| a | 1 |\n"
    calls: list[tuple[str, int]] = []
    out = _tier(docling_md, calls).refine(_ctx("Table 3: Costs", page_index=58), page)
    assert out.tier == "docling-table"
    assert FLAG_DOCLING_INVOKED in out.flags
    assert FLAG_TEXTURE_RECOVERED in out.flags  # preserved
    assert "| metric | value |" in out.markdown
    assert "trailing" in out.markdown  # prose preserved
    assert calls == [("x.pdf", 58)]
