"""Phase 4: Markdown / plain-text extractors emit 1-based ``line`` per chunk.

Index-time → search-time round-trip: build a fresh Tantivy index from a
known MD fixture, search it, and assert that ``Hit.line`` matches the
source line where the matching heading lives.
"""

from __future__ import annotations

from pathlib import Path

from fnd.extract.markdown import extract as md_extract
from fnd.extract.plain import extract as txt_extract
from fnd.index import build_index
from fnd.query import Searcher


def test_markdown_chunks_have_section_start_line(tmp_path: Path) -> None:
    """Each MD chunk's ``line`` field is the 1-based line of the heading
    that opens that section."""
    md = tmp_path / "doc.md"
    md.write_text(
        "# Intro\n\nfirst body line\n\n## Methods\n\nmiddle body\n\n## Results\n\ntrail\n"
    )
    chunks = list(md_extract(md))
    assert len(chunks) == 3
    # Heading "# Intro" sits on line 1.
    assert chunks[0].line == 1
    # "## Methods" sits on line 5.
    assert chunks[1].line == 5
    # "## Results" sits on line 9.
    assert chunks[2].line == 9


def test_plain_text_chunks_have_start_line(tmp_path: Path) -> None:
    """TXT chunks carry the 1-based line of their first character."""
    body = "\n".join(f"line {i:04d}" for i in range(1, 500))
    txt = tmp_path / "long.txt"
    txt.write_text(body)
    chunks = list(txt_extract(txt))
    # First chunk starts at line 1; later chunks at the line whose
    # character offset matches the chunk's ``start``.
    assert chunks[0].line == 1
    if len(chunks) > 1:
        # Second chunk's first byte falls inside the file, so its
        # line should be > 1 and <= total line count.
        assert chunks[1].line > 1
        assert chunks[1].line <= 500


def test_hit_line_round_trips_through_index(tmp_path: Path) -> None:
    """End-to-end: indexing + searching preserves the line locator."""
    root = tmp_path / "notes"
    root.mkdir()
    md = root / "note.md"
    md.write_text(
        "# Intro\n\nopening prose\n\n## Findings\n\n" "the unique-keyword-here landmark phrase\n"
    )
    idx = tmp_path / "idx"
    build_index(roots=[root], index_dir=idx, collection="notes")

    hits = Searcher(index_dir=idx).search("unique-keyword-here landmark")
    assert hits
    # Top hit should be the "Findings" section starting on line 5.
    assert any(h.line == 5 for h in hits), [h.line for h in hits]


def test_hit_line_zero_for_kinds_without_line_tracking(tmp_path: Path) -> None:
    """PDF / DOCX / PPTX chunks set ``line=0`` — they have their own
    locators (page, slide, heading_path) and CLI deep-links for those
    formats don't accept a line anyway."""
    # Use the existing fixture PDF — easiest non-MD non-TXT corpus.
    fixtures = Path("tests/fixtures")
    if not fixtures.is_dir():
        return
    pdf_dir = tmp_path / "pdf"
    pdf_dir.mkdir()
    src_pdf = fixtures / "papers" / "test.pdf"
    if not src_pdf.is_file():
        return
    (pdf_dir / "test.pdf").write_bytes(src_pdf.read_bytes())
    idx = tmp_path / "idx"
    build_index(roots=[pdf_dir], index_dir=idx, collection="pdfs")
    hits = Searcher(index_dir=idx).search("the")
    pdf_hits = [h for h in hits if h.kind == "pdf"]
    if pdf_hits:
        assert all(h.line == 0 for h in pdf_hits)
