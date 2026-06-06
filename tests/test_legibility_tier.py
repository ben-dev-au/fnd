"""Phase D (deferred): LegibilityReprocessTier + LegibilityEvaluator.

Built and unit-tested in isolation; deliberately NOT composed into the
live pipeline (the measured corpus has uniformly legible baked OCR). The
final test pins that exclusion so a future wiring is a conscious choice.
"""

from __future__ import annotations

import pytest

from fnd.extract.recovery import (
    FLAG_DOCLING_INVOKED,
    FLAG_LOW_QUALITY,
    ExtractionContext,
    LegibilityEvaluator,
    LegibilityReprocessTier,
    PageExtraction,
)

# Small deterministic dictionary so tests don't depend on the system list.
_DICT = {
    "the",
    "pattern",
    "provides",
    "interface",
    "for",
    "creating",
    "objects",
    "and",
    "good",
    "text",
    "clean",
    "prose",
    "reads",
    "well",
}


def _ev() -> LegibilityEvaluator:
    return LegibilityEvaluator(dictionary=_DICT)


def _ctx() -> ExtractionContext:
    return ExtractionContext(doc=object(), page=object(), page_index=7, path="x.pdf", flat="")


# ── LegibilityEvaluator ──────────────────────────────────────────────────────
def test_legr_excludes_code_fences() -> None:
    ev = _ev()
    # the fenced gibberish must not drag the prose score down
    legr, tokens = ev.prose_legr("```\nzz qq xx vv\n```\nthe pattern provides good clean prose")
    assert legr == pytest.approx(1.0)
    assert tokens == 6


def test_legr_camelcase_counts_as_legible() -> None:
    ev = LegibilityEvaluator(dictionary=set())  # empty dict isolates the camel rule
    legr, _ = ev.prose_legr("AbstractFactory HTTPServer createWidget")
    assert legr == pytest.approx(1.0)


def test_legr_none_when_no_prose() -> None:
    assert _ev().prose_legr("```\ncode only\n```")[0] is None


def test_legr_abstains_when_system_dict_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """No injected dict + an absent system word list (Windows / minimal
    container) must abstain (None), not score every page 0.0 — otherwise
    the tier would reprocess all legible prose."""
    from fnd.extract.recovery import evaluators

    monkeypatch.setattr(evaluators, "_load_system_dict", set)
    ev = LegibilityEvaluator()  # lazy load → hits the patched (empty) loader
    legr, tokens = ev.prose_legr("the pattern provides good clean prose")
    assert legr is None
    assert tokens == 6  # prose still counted, just unjudged


def test_legr_suffix_stemming() -> None:
    ev = LegibilityEvaluator(dictionary={"object", "pattern"})
    legr, _ = ev.prose_legr("objects patterns")  # both recovered by the 's' suffix
    assert legr == pytest.approx(1.0)


# ── LegibilityReprocessTier ──────────────────────────────────────────────────
# 35 lowercase non-dictionary, non-CamelCase tokens → legr 0.0, enough
# prose to clear the 30-token floor.
_GARBLED = " ".join(["zzqq"] * 35)
_CLEAN = "the pattern provides interface for creating objects and good clean text reads well " * 3


def _tier(
    ev: LegibilityEvaluator, ocr: str, docling: str, log: dict[str, int] | None = None
) -> LegibilityReprocessTier:
    def native_ocr(_page: object) -> str:
        if log is not None:
            log["ocr"] = log.get("ocr", 0) + 1
        return ocr

    def docling_extract(_path: str, _pi: int) -> str:
        if log is not None:
            log["docling"] = log.get("docling", 0) + 1
        return docling

    return LegibilityReprocessTier(ev, native_ocr, docling_extract)


def test_tier_passes_through_legible_prose() -> None:
    log: dict[str, int] = {}
    out = _tier(_ev(), "x", "y", log).refine(_ctx(), PageExtraction(markdown=_CLEAN))
    assert out.markdown == _CLEAN
    assert log == {}  # no reprocessing attempted


def test_tier_skips_when_too_little_prose() -> None:
    log: dict[str, int] = {}
    short = "zz qq xx"  # garbled but <30 tokens
    out = _tier(_ev(), "x", "y", log).refine(_ctx(), PageExtraction(markdown=short))
    assert out.markdown == short
    assert log == {}


def test_tier_prefers_native_ocr_when_it_lifts_legibility() -> None:
    log: dict[str, int] = {}
    out = _tier(_ev(), _CLEAN, "z", log).refine(_ctx(), PageExtraction(markdown=_GARBLED))
    assert out.markdown == _CLEAN
    assert out.tier == "legibility-ocr"
    assert log == {"ocr": 1}  # docling never reached


def test_tier_falls_back_to_docling_then_low_quality() -> None:
    log: dict[str, int] = {}
    # native OCR no better (still garbled); docling recovers clean prose
    out = _tier(_ev(), _GARBLED, _CLEAN, log).refine(_ctx(), PageExtraction(markdown=_GARBLED))
    assert out.tier == "legibility-docling"
    assert FLAG_DOCLING_INVOKED in out.flags
    assert log == {"ocr": 1, "docling": 1}


def test_tier_flags_low_quality_when_nothing_helps() -> None:
    out = _tier(_ev(), _GARBLED, _GARBLED).refine(_ctx(), PageExtraction(markdown=_GARBLED))
    assert FLAG_LOW_QUALITY in out.flags
    assert out.markdown == _GARBLED  # text left untouched


# ── deferred: not composed ───────────────────────────────────────────────────
def test_legibility_tier_not_in_live_pipeline() -> None:
    from fnd.extract import pdf

    tiers = pdf._recovery_pipeline()._tiers
    assert not any(isinstance(t, LegibilityReprocessTier) for t in tiers)
    assert [type(t).__name__ for t in tiers] == [
        "ProductionLayoutTier",
        "LigatureRepairTier",
        "InvisibleTextTier",
        "DoclingTableTier",
        "FlatFallbackTier",
    ]
