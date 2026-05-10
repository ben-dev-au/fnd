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


def _build_pdf_with_printed_numbers(path: Path, *, body_count: int, body_start: int) -> None:
    """A PDF whose body pages print ``body_start..body_start+N-1`` in
    the bottom margin — the typical typeset-book layout — and whose
    front matter has no margin number at all (mimicking unlabeled
    front matter you'd find in most real books).

    No ``set_page_labels`` is called, so the metadata-based label
    lookup returns "" and the extractor must fall back to the
    margin-scan heuristic.
    """
    doc = pymupdf.open()
    # 2 prefatory pages with body text but no margin number.
    for n in range(2):
        page = doc.new_page(width=612, height=792)
        page.insert_text((72, 100), f"front matter line {n}", fontsize=12, fontname="helv")
    # Body pages: real text in the middle, the printed page number
    # alone in the bottom margin (a few lines above the page edge).
    for i in range(body_count):
        page = doc.new_page(width=612, height=792)
        page.insert_text((72, 200), "body of the chapter goes here", fontsize=12, fontname="helv")
        page.insert_text(
            (300, 770),  # bottom margin, centered-ish
            str(body_start + i),
            fontsize=10,
            fontname="helv",
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(path))
    doc.close()


def test_extract_falls_back_to_margin_scan_when_no_label_metadata(tmp_path: Path) -> None:
    """The much-more-common case: the PDF prints page numbers in the
    margin but never declared explicit labels. The extractor's
    heuristic should pick those up so the displayed locator stops
    being off by the front-matter count."""
    pdf = tmp_path / "book.pdf"
    _build_pdf_with_printed_numbers(pdf, body_count=3, body_start=1)

    chunks = list(extract(pdf))
    assert len(chunks) == 5  # 2 front matter + 3 body

    # Front matter has no printed number → empty label, falls back
    # to PDF index in display.
    assert chunks[0].page_label == ""
    assert chunks[1].page_label == ""
    # Body pages reflect the *printed* number, not the PDF index.
    assert chunks[2].page_label == "1"
    assert chunks[3].page_label == "2"
    assert chunks[4].page_label == "3"
    # And page (PDF index) is unchanged so Skim navigation still
    # works.
    assert [c.page for c in chunks] == [1, 2, 3, 4, 5]


def test_margin_scan_ignores_numbers_in_body_text(tmp_path: Path) -> None:
    """Don't mistake a number that happens to appear in the middle of
    the page for the printed page number."""
    pdf = tmp_path / "no-margins.pdf"
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    # A naked integer right in the middle of the page, plus body text.
    page.insert_text((300, 396), "42", fontsize=12, fontname="helv")
    page.insert_text((72, 500), "this is body text", fontsize=12, fontname="helv")
    doc.save(str(pdf))
    doc.close()

    chunks = list(extract(pdf))
    assert len(chunks) == 1
    # Margin scan must NOT have picked up the centred "42".
    assert chunks[0].page_label == ""
