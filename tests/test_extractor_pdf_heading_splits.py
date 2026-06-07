"""Each detected PDF heading starts its own chunk.

Pre-fix the PDF extractor emitted exactly one chunk per page and kept only
the *first* largest-font span as that page's ``heading_path``. Consecutive
same-size headings on one page were therefore merged: the second heading
(and its prose) was absorbed into the first heading's chunk, so a search
that hit the second section surfaced the wrong heading and the preview
jumped to the wrong place.
"""

from __future__ import annotations

import os
from pathlib import Path

import pymupdf
import pytest

from fnd.extract.pdf import extract

_HEAD_1 = "Strong authentication and key management"
_BODY_1 = (
    "Alpha mitigation strengthens identity and certificate handling.\n"
    "It enforces multi factor access for administrative endpoints.\n"
    "Robust rotation and revocation processes protect stored keys."
)
_HEAD_2 = "Monitoring segmentation and defence in depth"
_BODY_2 = (
    "Bravo strategy designs the network around layered controls.\n"
    "Segmenting critical systems limits the blast radius of failures.\n"
    "Intrusion detection and logging surface anomalies for review."
)


def _make_two_heading_page(path: Path) -> None:
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 90), _HEAD_1, fontsize=20)
    page.insert_text((72, 120), _BODY_1, fontsize=11)
    page.insert_text((72, 320), _HEAD_2, fontsize=20)
    page.insert_text((72, 350), _BODY_2, fontsize=11)
    doc.save(str(path))
    doc.close()


def test_consecutive_headings_split_into_own_chunks(tmp_path: Path) -> None:
    pdf_path = tmp_path / "two_headings.pdf"
    _make_two_heading_page(pdf_path)

    chunks = [c for c in extract(pdf_path) if c.heading_path]

    by_leaf = {c.heading_path.split(" > ")[-1]: c for c in chunks}
    assert _HEAD_1 in by_leaf, [c.heading_path for c in chunks]
    assert _HEAD_2 in by_leaf, [c.heading_path for c in chunks]

    c1, c2 = by_leaf[_HEAD_1], by_leaf[_HEAD_2]
    # Each section's searchable body holds only its own prose.
    assert "Alpha mitigation" in c1.body
    assert "Bravo strategy" not in c1.body
    assert "Bravo strategy" in c2.body
    assert "Alpha mitigation" not in c2.body
    # Distinct chunks → distinct chunk_seq (the TUI/dedup identity key).
    assert c1.chunk_seq != c2.chunk_seq


# Path to a real PDF whose numbered-heading list reproduces the merge bug,
# supplied out-of-band so no machine-specific path is committed.
_SAMPLE_ENV = os.environ.get("FND_AUDIT_PDF_SAMPLE", "")
_SAMPLE = Path(_SAMPLE_ENV) if _SAMPLE_ENV else None


@pytest.mark.skipif(
    not (_SAMPLE and _SAMPLE.exists() and os.environ.get("FND_AUDIT_REAL_CORPUS")),
    reason="real PDF sample absent (set FND_AUDIT_PDF_SAMPLE=<path> FND_AUDIT_REAL_CORPUS=1)",
)
def test_real_sample_item3_is_own_chunk() -> None:
    from fnd.extract import pdf as pdfx

    pdfx.set_force_fresh_texture(True)
    try:
        chunks = list(pdfx.extract(_SAMPLE))
    finally:
        pdfx.set_force_fresh_texture(False)

    item3 = [c for c in chunks if "monitoring, segmentation and defence" in c.heading_path.lower()]
    assert item3, [c.heading_path for c in chunks if c.page == 4]
    # And item-3 prose no longer bleeds into item-2's chunk.
    item2 = next(c for c in chunks if "strong authentication" in c.heading_path.lower())
    assert "monitoring, segmentation and defence in depth" not in item2.body.lower()
