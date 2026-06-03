"""Phase A: coverage gate + InvisibleTextTier (content + code recovery).

Unit tests drive the gate branches with fakes; integration tests prove
the real ignore_alpha lever recovers a synthetic invisible-text page
(scanned-OCR mimic) through the full extraction pipeline, while leaving
born-digital output byte-identical.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fnd.extract.recovery import (
    FLAG_TEXTURE_RECOVERED,
    CoverageEvaluator,
    ExtractionContext,
    InvisibleTextTier,
    PageExtraction,
)
from fnd.extract.recovery.tiers import _clamp_headings

FIXTURE = Path(__file__).parent / "fixtures" / "scanned" / "invisible.pdf"


def _ctx(flat: str) -> ExtractionContext:
    return ExtractionContext(doc=object(), page=object(), page_index=0, path="x.pdf", flat=flat)


def _distinct_words(n: int) -> str:
    """``n`` distinct 3-letter alpha tokens (the gate's tokenizer needs
    >=3 letters and counts distinct tokens, so digits/repeats won't do)."""
    import string

    a = string.ascii_lowercase
    return " ".join(a[i // 676] + a[(i // 26) % 26] + a[i % 26] for i in range(n))


# ── CoverageEvaluator ────────────────────────────────────────────────────────
def test_coverage_fraction_of_flat_tokens_present() -> None:
    ev = CoverageEvaluator()
    assert ev.coverage("alpha beta", "alpha beta gamma delta") == pytest.approx(0.5)
    assert ev.coverage("", "alpha beta") == 0.0
    assert ev.coverage("anything", "") == 0.0  # nothing to cover


def test_flat_token_count_distinct_three_letter_runs() -> None:
    ev = CoverageEvaluator()
    # "to" and "a" are <3 letters and excluded; "the" repeats (distinct).
    assert ev.flat_token_count("the cat sat on the mat to a") == 4


# ── _clamp_headings ──────────────────────────────────────────────────────────
def test_clamp_demotes_headings_to_h2() -> None:
    assert _clamp_headings("# Title\n### Sub\nbody") == "## Title\n## Sub\nbody"


def test_clamp_leaves_hash_comments_inside_fences_untouched() -> None:
    md = "```\n# not a heading\ncode = 1\n```\n# Real Heading"
    out = _clamp_headings(md)
    assert "# not a heading" in out  # untouched inside the fence
    assert out.endswith("## Real Heading")


# ── InvisibleTextTier gate (fakes) ───────────────────────────────────────────
def _tier(new_md: str, calls: list[int] | None = None) -> InvisibleTextTier:
    def fake_invisible(_doc: object, page_index: int) -> str:
        if calls is not None:
            calls.append(page_index)
        return new_md

    return InvisibleTextTier(fake_invisible, CoverageEvaluator())


def test_gate_skips_when_production_empty() -> None:
    calls: list[int] = []
    out = _tier("recovered text here plenty", calls).refine(
        _ctx("a " * 30), PageExtraction(markdown="")
    )
    assert out.markdown == ""
    assert calls == []  # never re-extracts


def test_gate_skips_below_token_floor() -> None:
    """Too few flat tokens to judge coverage — leave production alone."""
    calls: list[int] = []
    flat = "one two three four five six seven eight nine ten"  # 10 tokens < 20
    out = _tier("xxx", calls).refine(_ctx(flat), PageExtraction(markdown="prod"))
    assert out.markdown == "prod"
    assert calls == []


def test_gate_skips_when_coverage_above_floor() -> None:
    """Born-digital: production already covers the flat layer — no fallback."""
    calls: list[int] = []
    flat = _distinct_words(30)
    prod = flat  # 100% coverage
    out = _tier("xxx", calls).refine(_ctx(flat), PageExtraction(markdown=prod))
    assert out.markdown == prod
    assert out.coverage == pytest.approx(1.0)
    assert calls == []


def test_gate_fires_and_keeps_higher_coverage_variant() -> None:
    flat = _distinct_words(30)
    prod = " ".join(flat.split()[:2])  # ~0.067 coverage, below the 0.70 floor
    calls: list[int] = []
    out = _tier(flat, calls).refine(_ctx(flat), PageExtraction(markdown=prod))
    assert out.markdown == flat
    assert out.tier == "invisible-text"
    assert out.coverage == pytest.approx(1.0)
    assert FLAG_TEXTURE_RECOVERED in out.flags
    assert calls == [0]  # re-extracted exactly once


def test_gate_fires_but_keeps_production_when_fallback_worse() -> None:
    flat = _distinct_words(30)
    prod = " ".join(flat.split()[:3])  # below floor but beats the empty fallback
    out = _tier("", None).refine(_ctx(flat), PageExtraction(markdown=prod))
    assert out.markdown == prod  # production retained
    assert out.tier != "invisible-text"


# ── Integration: real lever on the invisible-text fixture ────────────────────
@pytest.mark.skipif(not FIXTURE.exists(), reason="invisible-text fixture not built")
def test_pipeline_recovers_invisible_prose_and_code() -> None:
    pytest.importorskip("pymupdf4llm")
    from fnd.extract import pdf

    chunks = {c.chunk_seq: c for c in pdf.extract(FIXTURE)}
    assert set(chunks) == {0, 1}

    prose = chunks[0].body_md
    assert "quicksort" in prose.lower()
    assert "recursively" in prose.lower()
    # heading recovered but demoted below fnd's section breadcrumb: no
    # bare H1 line survives, only the clamped ## form.
    assert "## Visible Heading Only" in prose
    assert not any(line.startswith("# ") for line in prose.splitlines())

    code = chunks[1].body_md
    assert "```" in code  # recovered as a fenced code block
    assert "RingBuffer" in code
    assert "dequeue" in code


@pytest.mark.skipif(not FIXTURE.exists(), reason="invisible-text fixture not built")
def test_recovered_coverage_beats_production() -> None:
    pytest.importorskip("pymupdf4llm")
    import pymupdf  # type: ignore[import-not-found]

    from fnd.extract import pdf

    ev = CoverageEvaluator()
    doc = pymupdf.open(str(FIXTURE))
    try:
        for i in range(doc.page_count):
            flat = doc[i].get_text("text") or ""
            old_cov = ev.coverage(pdf._extract_page_md(doc, i), flat)
            new_cov = ev.coverage(pdf._extract_invisible_md(doc, i), flat)
            assert old_cov < 0.70, f"page {i} production cov should trip the gate"
            assert new_cov > old_cov, f"page {i} fallback should recover more"
    finally:
        doc.close()
