"""Unit tests for the recovery pipeline primitives in isolation."""

from __future__ import annotations

from fnd.extract.recovery import (
    ExtractionContext,
    PageExtraction,
    PageRecoveryPipeline,
    ProductionLayoutTier,
)


def _ctx(page_index: int = 0, flat: str = "") -> ExtractionContext:
    return ExtractionContext(
        doc=object(), page=object(), page_index=page_index, path="x.pdf", flat=flat
    )


def test_production_tier_wraps_injected_extractor() -> None:
    """ProductionLayoutTier returns the injected extractor's output and
    stamps its provenance."""
    calls: list[tuple[object, int]] = []

    def fake_extract(doc: object, page_index: int) -> str:
        calls.append((doc, page_index))
        return f"# page {page_index}"

    tier = ProductionLayoutTier(fake_extract)
    out = tier.refine(_ctx(page_index=3), PageExtraction(markdown=""))
    assert out.markdown == "# page 3"
    assert out.tier == "production-layout"
    assert len(calls) == 1
    assert calls[0][1] == 3


def test_pipeline_folds_tiers_in_order() -> None:
    """Each tier sees the previous result; the last to change markdown
    wins."""

    class Append:
        def __init__(self, suffix: str) -> None:
            self.suffix = suffix

        def refine(self, ctx: ExtractionContext, current: PageExtraction) -> PageExtraction:
            return PageExtraction(markdown=current.markdown + self.suffix, tier=self.suffix)

    pipe = PageRecoveryPipeline([Append("a"), Append("b")])
    out = pipe.recover(_ctx())
    assert out.markdown == "ab"
    assert out.tier == "b"


def test_pipeline_passthrough_tier_keeps_current() -> None:
    """A tier that doesn't apply returns ``current`` unchanged."""

    class Noop:
        def refine(self, ctx: ExtractionContext, current: PageExtraction) -> PageExtraction:
            return current

    seed = ProductionLayoutTier(lambda _doc, i: f"seed{i}")
    pipe = PageRecoveryPipeline([seed, Noop()])
    out = pipe.recover(_ctx(page_index=2))
    assert out.markdown == "seed2"
    assert out.tier == "production-layout"
