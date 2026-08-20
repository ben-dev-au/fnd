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


def test_passthrough_on_complete_coverage() -> None:
    """A fully-captured page is never touched, and the flat blocks are not
    even queried — the one exact short-circuit: if every flat token is in the
    Markdown, no block can be missing."""
    flat = _distinct_words(0, 40)
    calls: list[int] = []
    current = PageExtraction(markdown=flat, tier="production-layout")
    out = _tier([flat], calls).refine(_ctx(flat), current)
    assert out.markdown == flat
    assert out.tier == "production-layout"
    assert out.coverage == pytest.approx(1.0)
    assert calls == []


def test_backfills_a_dropped_block_on_a_well_covered_page() -> None:
    """The regression this tier's old 0.90 page-level floor allowed through.

    A page can be 92% covered and still have lost a whole block — searchable
    prose the preview could never show. Block-level evidence decides now, not
    a page-level ratio.
    """
    kept = _distinct_words(0, 46)
    dropped = _distinct_words(46, 4)  # 4/50 tokens -> coverage 0.92, above the old floor
    flat = kept + " " + dropped
    current = PageExtraction(markdown=kept, tier="production-layout")

    out = _tier([kept, dropped]).refine(_ctx(flat), current)

    assert out.coverage == pytest.approx(1.0)
    assert out.tier == "flat-fallback"
    assert dropped in out.markdown
    assert out.markdown.startswith(kept)
    assert FLAG_TEXTURE_RECOVERED in out.flags


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


def test_flat_fallback_is_not_a_pipeline_tier() -> None:
    """Flat-fallback runs in _finalise_body_md AFTER the born-digital
    docling fallback, not inside the recovery pipeline — so the prose it
    backfills can't be discarded by a later docling swap, and a
    flat-appended 'Table N' can't retro-trigger that docling."""
    from fnd.extract import pdf

    order = [type(t).__name__ for t in pdf._recovery_pipeline()._tiers]
    assert "FlatFallbackTier" not in order
    assert isinstance(pdf._flat_fallback_tier, FlatFallbackTier)


def test_finalise_runs_docling_check_before_flat_backfill(monkeypatch: pytest.MonkeyPatch) -> None:
    """The born-digital docling decision must see the structured markdown
    BEFORE flat-fallback appends — otherwise a backfilled 'Table 1' block
    could retro-trigger docling. Assert the markdown handed to
    _needs_docling_fallback carries no flat-appended prose, and that
    flat-fallback still backfills afterwards."""
    from fnd.extract import pdf

    seen: dict[str, str] = {}

    def fake_needs(page: object, md: str) -> bool:
        seen["md"] = md
        return False  # don't run docling; we only care what it inspected

    class FakeRect:
        width = height = 100.0

    class FakePage:
        rect = FakeRect()

        def get_text(self, _kind: str, sort: bool = False) -> list[tuple[object, ...]]:
            return [(0, 0, 1, 1, "Table 1 lists the dropped prose tokens", 0, 0)]

    monkeypatch.setattr(pdf, "_needs_docling_fallback", fake_needs)
    flat = "heading only " + "Table 1 lists the dropped prose tokens " + _distinct_words(0, 30)
    ctx = ExtractionContext(doc=object(), page=FakePage(), page_index=0, path="x.pdf", flat=flat)

    result = pdf._finalise_body_md(ctx, "## Heading only")

    assert "Table 1" not in seen["md"]  # docling inspected the pre-backfill md
    assert "dropped prose tokens" in result  # flat-fallback ran afterwards
