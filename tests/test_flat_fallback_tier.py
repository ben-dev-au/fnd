"""Flat-fallback tier: backfill prose blocks the layout parser dropped on
graphically-complex born-digital pages (CyberCX threat report), so every
indexed (flat-layer) token is present in the preview texture.

The tier keeps the structured Markdown and appends ONLY the flat blocks it
missed (in reading order) — it is not a wholesale flat replacement, so a
well-extracted page is left untouched.
"""

from __future__ import annotations

import pytest

from fnd.extract.recovery import (
    FLAG_TEXTURE_RECOVERED,
    CoverageEvaluator,
    ExtractionContext,
    FlatFallbackTier,
    PageExtraction,
)


def _ctx(flat: str) -> ExtractionContext:
    return ExtractionContext(doc=object(), page=object(), page_index=0, path="x.pdf", flat=flat)


def _distinct_words(start: int, n: int) -> str:
    """``n`` distinct 3-letter alpha tokens beginning at index ``start``."""
    import string

    a = string.ascii_lowercase
    return " ".join(a[i // 676] + a[(i // 26) % 26] + a[i % 26] for i in range(start, start + n))


def _tier(blocks: list[str], calls: list[int] | None = None) -> FlatFallbackTier:
    def fake_blocks(_page: object) -> list[str]:
        if calls is not None:
            calls.append(1)
        return blocks

    return FlatFallbackTier(CoverageEvaluator(), fake_blocks)


def test_passthrough_when_coverage_meets_floor() -> None:
    """A well-extracted page (cov >= floor) is never touched, and the flat
    blocks are not even queried."""
    flat = _distinct_words(0, 40)
    calls: list[int] = []
    current = PageExtraction(markdown=flat, tier="production-layout")
    out = _tier([flat], calls).refine(_ctx(flat), current)
    assert out.markdown == flat
    assert out.tier == "production-layout"
    assert out.coverage == pytest.approx(1.0)
    assert calls == []


def test_skips_below_token_floor() -> None:
    flat = "one two three four five"  # < 20 distinct tokens
    out = _tier(["anything"]).refine(_ctx(flat), PageExtraction(markdown="prod"))
    assert out.markdown == "prod"


def test_backfills_a_fully_dropped_block() -> None:
    """The layout parser kept a small caption but dropped the main prose;
    the dropped block is appended and coverage is restored."""
    kept = _distinct_words(0, 3)  # tiny fragment -> low coverage
    dropped = _distinct_words(3, 37)
    flat = kept + " " + dropped
    current = PageExtraction(markdown=kept, tier="production-layout")
    out = _tier([kept, dropped]).refine(_ctx(flat), current)
    assert out.tier == "flat-fallback"
    assert dropped in out.markdown
    assert out.markdown.startswith(kept)  # structure preserved, fill appended
    assert out.coverage == pytest.approx(1.0)
    assert FLAG_TEXTURE_RECOVERED in out.flags


def test_does_not_duplicate_blocks_already_present() -> None:
    """A block whose tokens are already in the structured md is not
    re-appended (no duplication), even when another block triggers the
    gap-fill."""
    shown = _distinct_words(0, 20)
    dropped = _distinct_words(20, 20)
    flat = shown + " " + dropped
    # md already contains `shown` (formatted) but lost `dropped`.
    current = PageExtraction(markdown="## Heading\n\n" + shown, tier="production-layout")
    out = _tier([shown, dropped]).refine(_ctx(flat), current)
    assert out.tier == "flat-fallback"
    assert out.markdown.count(shown.split()[0]) == 1  # shown block not duplicated
    assert dropped in out.markdown


def test_appends_dropped_blocks_in_reading_order() -> None:
    a = _distinct_words(0, 15)
    b = _distinct_words(15, 15)
    flat = a + " " + b
    out = _tier([a, b]).refine(_ctx(flat), PageExtraction(markdown="x", tier="production-layout"))
    assert out.markdown.index(a) < out.markdown.index(b)


def test_no_op_when_nothing_missing() -> None:
    """Below the floor by a hair, but every block is already represented:
    no fill, structured md retained."""
    flat = _distinct_words(0, 40)
    # md holds ~0.88 of tokens: below floor, but each block mostly present.
    current = PageExtraction(markdown=_distinct_words(0, 35), tier="production-layout")
    out = _tier([flat]).refine(_ctx(flat), current)
    assert out.tier == "production-layout"  # untouched
    assert _distinct_words(0, 35) in out.markdown


def test_pipeline_runs_docling_table_before_flat_fallback() -> None:
    """Composition invariant: flat-fallback is the LAST tier, after the
    docling-table tier. Its FLAG_TEXTURE_RECOVERED therefore can't
    retro-trigger the docling tier, so the two never double-process a page
    (verified end-to-end on pages that legitimately need both)."""
    from fnd.extract import pdf
    from fnd.extract.recovery import (
        DoclingTableTier,
        InvisibleTextTier,
        LigatureRepairTier,
        ProductionLayoutTier,
    )

    order = [type(t) for t in pdf._recovery_pipeline()._tiers]
    assert order == [
        ProductionLayoutTier,
        LigatureRepairTier,
        InvisibleTextTier,
        DoclingTableTier,
        FlatFallbackTier,
    ]
