"""Phase B: per-page hdr_info recovers mid-size subheads.

pymupdf4llm scans the whole document to rank header font sizes; in a
scanned book the many distinct divider fonts exhaust the 6-level cutoff
and a genuine mid-size subhead classifies as body. The headings fixture
reproduces that (six invisible divider pages + a 16pt subhead on the
target page); per-page hdr_info recovers the subhead.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytest.importorskip("pymupdf4llm")
import pymupdf  # type: ignore[import-not-found]
import pymupdf4llm

from fnd.extract import pdf
from fnd.extract.pdf import _mute_fd, _per_page_hdr_info

FIXTURE = Path(__file__).parent / "fixtures" / "scanned" / "headings.pdf"
TARGET = 6  # the page carrying the mid-size subhead
pytestmark = pytest.mark.skipif(not FIXTURE.exists(), reason="headings fixture not built")

_HEADING_RE = re.compile(r"(?m)^#{1,6} .*")


def _invisible_to_md(doc: pymupdf.Document, page_index: int, hdr_info: object | None) -> str:
    """Run the ignore_alpha lever with an explicit hdr_info, mirroring
    ``_extract_invisible_md`` so the test can vary only the header scope."""
    prior = bool(getattr(pymupdf4llm, "_use_layout", True))
    try:
        pymupdf4llm.use_layout(False)
        with _mute_fd(1), _mute_fd(2):
            chunks = pymupdf4llm.to_markdown(
                doc,
                pages=[page_index],
                page_chunks=True,
                show_progress=False,
                ignore_alpha=True,
                force_text=True,
                ignore_images=True,
                ignore_graphics=False,
                table_strategy="lines",
                hdr_info=hdr_info,
            )
    finally:
        pymupdf4llm.use_layout(prior)
    return str(chunks[0].get("text", "")) if chunks else ""


def test_fixture_reproduces_docwide_heading_loss() -> None:
    """Sanity: the document-wide scan really does drop the subhead, so the
    per-page recovery below is a genuine fix, not a no-op."""
    doc = pymupdf.open(str(FIXTURE))
    try:
        docwide = _invisible_to_md(doc, TARGET, None)
    finally:
        doc.close()
    assert _HEADING_RE.search(docwide) is None


def test_per_page_hdr_info_recovers_subhead() -> None:
    doc = pymupdf.open(str(FIXTURE))
    try:
        perpage = _invisible_to_md(doc, TARGET, _per_page_hdr_info(doc, TARGET))
    finally:
        doc.close()
    headings = _HEADING_RE.findall(perpage)
    assert any("Implementation Notes" in h for h in headings)


def test_pipeline_emits_clamped_heading() -> None:
    """End to end: the recovered subhead reaches body_md demoted to ##."""
    chunks = {c.chunk_seq: c for c in pdf.extract(FIXTURE)}
    body = chunks[TARGET].body_md
    assert "## Implementation Notes" in body
    assert "dispatcher" in body.lower()
    assert not any(line.startswith("# ") for line in body.splitlines())
