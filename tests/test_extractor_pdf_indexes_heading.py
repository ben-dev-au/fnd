"""PDF extractor folds each page's own heading into the searchable ``body``.

Pre-fix, ``body`` was the raw page text only; the page heading (derived
from the embedded TOC, or from font-size clustering) went to
``heading_path`` but never into ``body``. Since search gates visibility
on ``body``, a TOC section heading that isn't rendered verbatim in the
page text was unsearchable — unlike markdown/docx/pptx, which all fold a
chunk's own heading into ``body``.
"""

from __future__ import annotations

from pathlib import Path

import pymupdf

from fnd.extract.pdf import extract


def _make_pdf_with_toc(path: Path) -> None:
    doc = pymupdf.open()
    p1 = doc.new_page()
    p1.insert_text((72, 72), "Introduction body text alpha.")
    p2 = doc.new_page()
    # Page 2's rendered text deliberately does NOT contain the heading.
    p2.insert_text((72, 72), "Body paragraph about widgets and gizmos.")
    # TOC bookmark whose title is absent from the page's rendered text.
    doc.set_toc([[1, "Hidden Section Heading Bravo", 2]])
    doc.save(str(path))
    doc.close()


def test_toc_heading_is_folded_into_body(tmp_path: Path) -> None:
    pdf_path = tmp_path / "toc.pdf"
    _make_pdf_with_toc(pdf_path)

    chunks = list(extract(pdf_path))
    page2 = next(c for c in chunks if c.page == 2)

    # Sanity: the heading was detected for this page.
    assert "Hidden Section Heading Bravo" in page2.heading_path
    # The fix: the page's own heading text is searchable via body.
    assert "Hidden Section Heading Bravo" in page2.body
    # Existing page text is still present.
    assert "widgets and gizmos" in page2.body
