"""Build the test-fixture corpus.

Run with ``uv run python -m tests.fixtures._build``. Outputs are committed so the
contract is reproducible without running this script — but running it must produce
byte-identical content (no timestamps in PDF metadata, no random ordering).

Anchor phrases are deliberately weird so they cannot appear naturally elsewhere.
Each phrase must appear in exactly one (file, page/section) so retrieval tests can
assert exact attribution.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pymupdf  # type: ignore[import-not-found]

FIXTURES = Path(__file__).parent

# (path, page-or-section, content) — anchor phrases must be unique across the corpus.
# The generator inserts them so the test layer knows where to find them.
PDF_PAGES: dict[int, str] = {
    1: "Introduction. This is the first page of the test PDF document.",
    2: "Background. We discuss the relevant prior work in this section.",
    3: "Definitions. Key terms used throughout this document.",
    4: "Method. The general approach taken in our research.",
    5: "Implementation details that explain how things were built.",
    6: "Preliminary results from initial experiments.",
    7: "Detailed analysis. The blue penguin sandwich was observed only here.",
    8: "Discussion of limitations and future work.",
    9: "Related work in the broader academic literature.",
    10: "Acknowledgements to all collaborators.",
    11: "Bibliography and references.",
    12: "Appendix with supplementary material and proofs.",
}

MD_CONTENT = textwrap.dedent("""\
    # Test Notes

    This is the top-level body of the notes file.

    ## Methodology

    Notes about how the methodology section is structured.

    ### Sampling

    Details about sampling. The ostrich firewall was triggered during pilot runs.

    ### Analysis

    How analysis was performed.

    ## Results

    Summary of results.

    ## Conclusion

    Wrap-up paragraph.
    """)

TXT_CONTENT = textwrap.dedent("""\
    A short plain text file used for the TXT extractor tests.

    The marigold compiler is mentioned only here in the corpus; queries for it should
    surface this file at rank 1.

    A second paragraph keeps the file from being trivially small.
    """)


def _write_pdf(path: Path, pages: dict[int, str]) -> None:
    doc = pymupdf.open()
    try:
        for page_no in sorted(pages):
            page = doc.new_page(width=612, height=792)
            page.insert_text(
                (72, 100),
                f"Page {page_no}",
                fontsize=18,
                fontname="helv",
            )
            page.insert_text(
                (72, 150),
                pages[page_no],
                fontsize=12,
                fontname="helv",
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        # garbage=4 + clean=True normalise output for reproducible bytes.
        doc.save(str(path), garbage=4, clean=True, deflate=True)
    finally:
        doc.close()


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def build() -> None:
    _write_pdf(FIXTURES / "papers" / "test.pdf", PDF_PAGES)
    _write_text(FIXTURES / "notes" / "index.md", MD_CONTENT)
    _write_text(FIXTURES / "plain" / "short.txt", TXT_CONTENT)


# ── Anchor table — the test contract ─────────────────────────────────────────
# Each entry is (path-relative-to-FIXTURES, kind, locator, phrase).
# `locator` is a page number for PDF and a heading_path for MD.
ANCHORS: list[tuple[str, str, object, str]] = [
    ("papers/test.pdf", "pdf", 7, "blue penguin sandwich"),
    ("notes/index.md", "md", "Test Notes > Methodology > Sampling", "ostrich firewall"),
    ("plain/short.txt", "txt", None, "marigold compiler"),
]


if __name__ == "__main__":
    build()
    print("✓ fixtures built")
