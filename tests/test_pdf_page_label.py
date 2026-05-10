"""PDF extractor populates ``page_label`` from the printed page label.

Books with prefatory pages (TOC, preface) typically label them in
roman numerals so the displayed locator matches what's actually
printed on the page, while ``page`` (PDF index) is what Skim needs
for deep-linking. The two diverge — that's the bug this guards
against.
"""

from __future__ import annotations

from pathlib import Path

import pymupdf  # type: ignore[import-not-found]

from acorn.extract.pdf import extract
from acorn.render import _chunk_header


def _build_labeled_pdf(path: Path) -> None:
    """A 5-page PDF: 2 prefatory pages labelled i, ii, then 3 body
    pages labelled 1, 2, 3 (so PDF index 3 == printed page "1")."""
    doc = pymupdf.open()
    for n in range(5):
        page = doc.new_page(width=612, height=792)
        page.insert_text((72, 100), f"body {n}", fontsize=12, fontname="helv")
    doc.set_page_labels(
        [
            {"startpage": 0, "prefix": "", "style": "r"},  # i, ii
            {"startpage": 2, "prefix": "", "style": "D"},  # 1, 2, 3
        ]
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(path))
    doc.close()


def _build_unlabeled_pdf(path: Path) -> None:
    """Plain 3-page PDF without page labels — extractor should leave
    ``page_label`` empty so display falls back to the PDF index."""
    doc = pymupdf.open()
    for n in range(3):
        page = doc.new_page(width=612, height=792)
        page.insert_text((72, 100), f"body {n}", fontsize=12, fontname="helv")
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(path))
    doc.close()


def test_extract_populates_page_label_for_labeled_pdf(tmp_path: Path) -> None:
    pdf = tmp_path / "book.pdf"
    _build_labeled_pdf(pdf)

    chunks = list(extract(pdf))
    assert len(chunks) == 5

    # Page index keeps the PDF-position semantic Skim needs.
    assert [c.page for c in chunks] == [1, 2, 3, 4, 5]
    # Printed labels: roman front matter, then numeric body.
    assert [c.page_label for c in chunks] == ["i", "ii", "1", "2", "3"]


def test_extract_leaves_page_label_empty_for_unlabeled_pdf(tmp_path: Path) -> None:
    pdf = tmp_path / "no-labels.pdf"
    _build_unlabeled_pdf(pdf)

    chunks = list(extract(pdf))
    assert len(chunks) == 3
    assert all(c.page_label == "" for c in chunks)
    assert [c.page for c in chunks] == [1, 2, 3]


def test_chunk_header_prefers_printed_label_over_page_index() -> None:
    """The ``p. N`` shown in previews / sidebar comes from the printed
    label whenever the PDF carries one."""

    class _C:
        page = 39
        page_label = "1"
        slide = 0
        heading_path = "Chapter 1 > Intro"
        chunk_seq = 38

    assert _chunk_header(_C()) == "p. 1 · Chapter 1 > Intro"


def test_chunk_header_falls_back_to_page_index_when_no_label() -> None:
    class _C:
        page = 7
        page_label = ""
        slide = 0
        heading_path = ""
        chunk_seq = 6

    assert _chunk_header(_C()) == "p. 7"
