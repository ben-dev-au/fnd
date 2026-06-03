"""Golden byte-identity net for the recovery-pipeline refactor.

The Bug-E fix routes structured extraction through a tiered
``PageRecoveryPipeline``. These tests pin born-digital output so the
refactor — and every later tier that gates on coverage — provably leaves
born-digital pages byte-for-byte unchanged. Born-digital coverage floors
well above the fallback gate, so no tier may ever rewrite these pages.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("pymupdf4llm")
import pymupdf  # type: ignore[import-not-found]

FIXTURE = Path(__file__).parent / "fixtures" / "papers" / "test.pdf"


def _direct_page_md() -> dict[int, str]:
    """The pre-refactor production transform per page: ``_extract_page_md``
    followed by ``_strip_picture_markers`` (no docling — born-digital
    test.pdf emits no picture-omitted markers, so the splice never runs)."""
    from fnd.extract import pdf

    doc = pymupdf.open(str(FIXTURE))
    try:
        return {i: pdf._strip_picture_markers(pdf._extract_page_md(doc, i)) for i in range(doc.page_count)}
    finally:
        doc.close()


def test_extract_body_md_matches_direct_production_path() -> None:
    """Black-box: ``extract()``'s body_md equals the direct production
    transform on every born-digital page. Holds before the refactor
    (inline call) and must keep holding after (pipeline) — and through
    Phase A, proving the coverage gate never fires on born-digital."""
    from fnd.extract import pdf

    expected = _direct_page_md()
    chunks = {c.chunk_seq: c.body_md for c in pdf.extract(FIXTURE)}
    assert chunks, "extract() must yield chunks"
    for page_index, body_md in chunks.items():
        assert body_md == expected[page_index], f"page {page_index} drifted from production path"
