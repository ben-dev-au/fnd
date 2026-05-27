"""Splice docling's recovered tables into pymupdf4llm's formatted page.

The fallback used to replace the whole page with docling output, losing
pymupdf4llm's bold/italic/headings — and on figure pages (no table)
gaining nothing. The splice keeps the formatted page and drops tables in
at the marker site, with three behaviours measured on the real corpus:
no-table keeps pymupdf4llm, clean 1:1 splices, ambiguous count falls
back to full replacement.
"""

from __future__ import annotations

from fnd.extract import pdf


def test_extract_md_tables_finds_contiguous_blocks() -> None:
    md = "intro\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\nmiddle prose\n\n| c | d |\n|---|---|\n"
    blocks = pdf._extract_md_tables(md)
    assert len(blocks) == 2
    assert blocks[0] == "| a | b |\n|---|---|\n| 1 | 2 |"
    assert blocks[1] == "| c | d |\n|---|---|"


def test_splice_keeps_formatting_and_inserts_table() -> None:
    """Clean 1:1: pymupdf formatting survives AND docling's table lands
    at the marker, with the bold wrapper removed (no orphan `**`)."""
    pymupdf_md = (
        "# Heading\n\n"
        "Some **bold** and *italic* prose.\n\n"
        "**==> picture [324 x 70] intentionally omitted <==**\n\n"
        "Trailing _formatted_ paragraph.\n"
    )
    docling_md = "## Recovered\n\n| col | val |\n|---|---|\n| a | 1 |\n"
    out = pdf._splice_docling_tables(pymupdf_md, docling_md)

    # pymupdf formatting preserved verbatim:
    assert "# Heading" in out
    assert "**bold**" in out
    assert "*italic*" in out
    assert "_formatted_" in out
    # docling table spliced in at the marker:
    assert "| col | val |" in out
    assert "| a | 1 |" in out
    # marker + its bold wrapper gone, no orphan `**`:
    assert "intentionally omitted" not in out
    assert "**==>" not in out
    assert "<==**" not in out


def test_splice_keeps_pymupdf_when_docling_finds_no_table() -> None:
    """The marker is a genuine figure/chart — docling recovers no table.
    Keep pymupdf4llm verbatim (formatting preserved); never replace with
    table-less docling output for no gain."""
    pymupdf_md = (
        "## **Figure 3**\n\n"
        "Caption with **bold** text.\n\n"
        "**==> picture [400 x 300] intentionally omitted <==**\n"
    )
    docling_md = "## Figure 3\n\nsome flat caption text, no table\n"
    out = pdf._splice_docling_tables(pymupdf_md, docling_md)
    assert out == pymupdf_md


def test_splice_keeps_pymupdf_when_it_already_has_a_table() -> None:
    """pymupdf4llm already rendered a vector table on the page but also
    emitted a redundant picture marker (the same table as an image).
    docling recovers no MISSED table here — splicing its copy would
    duplicate. Keep pymupdf4llm (table + formatting), no duplicate."""
    pymupdf_md = (
        "## **Acids**\n\n"
        "| Acid | M |\n|---|---|\n| Tartaric | 150 |\n\n"
        "**==> picture [337 x 260] intentionally omitted <==**\n"
    )
    docling_md = "| Acid | M |\n|---|---|\n| Tartaric | 150.09 |\n"
    out = pdf._splice_docling_tables(pymupdf_md, docling_md)
    assert out == pymupdf_md
    # exactly one table block survives (no duplicate):
    assert len(pdf._extract_md_tables(out)) == 1


def test_splice_full_replace_on_count_mismatch_with_tables() -> None:
    """Tables recovered but count doesn't match the markers (placement
    ambiguous) → full replacement so a recovered table is never dropped."""
    pymupdf_md = "**bold**\n\n==> picture [400 x 200] intentionally omitted <==\n"
    docling_md = "| a | b |\n|---|---|\n\nprose\n\n| c | d |\n|---|---|\n"
    out = pdf._splice_docling_tables(pymupdf_md, docling_md)
    assert out == docling_md


def test_splice_multiple_markers_in_reading_order() -> None:
    pymupdf_md = (
        "**==> picture [200 x 100] intentionally omitted <==**\n\n"
        "mid **bold**\n\n"
        "**==> picture [200 x 100] intentionally omitted <==**\n"
    )
    docling_md = "| t1 |\n|---|\n\n| t2 |\n|---|\n"
    out = pdf._splice_docling_tables(pymupdf_md, docling_md)
    assert "mid **bold**" in out
    assert "intentionally omitted" not in out
    # first marker -> first table, second -> second.
    assert out.index("| t1 |") < out.index("mid **bold**") < out.index("| t2 |")
