"""Verify the structured PDF extraction path when pdf-structure extra is present.

Requirements covered:
- F5: With extras installed, PDF chunks have populated body_md.

These tests are skipped when pymupdf4llm isn't available — i.e., they
run only after `uv sync --extra pdf-structure` (or the future
`fnd extras install pdf-structure`). CI without the extra still passes
because the tests are gated by pytest.importorskip.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_pymupdf4llm = pytest.importorskip("pymupdf4llm")

# Partial uninstalls leave pymupdf4llm importable but missing
# ``to_markdown`` — the extra is effectively broken in that state.
# Skip rather than fail; full reinstall fixes it.
if not hasattr(_pymupdf4llm, "to_markdown"):
    pytest.skip(
        "pymupdf4llm is importable but has no `to_markdown` — "
        "looks like a partial install. Run `fnd extras install "
        "pdf-structure` to restore.",
        allow_module_level=True,
    )

FIXTURE = Path(__file__).parent / "fixtures" / "papers" / "test.pdf"


def test_body_md_populated_when_extra_present() -> None:
    """F5: body_md should be non-empty when pymupdf4llm is importable."""
    from fnd.extract import pdf

    assert (
        pdf._HAS_PYMUPDF4LLM
    ), "extras present but _HAS_PYMUPDF4LLM is False — module detection bug"
    chunks = list(pdf.extract(FIXTURE))
    assert chunks, "extract() must yield chunks"
    populated = [c for c in chunks if c.body_md]
    assert len(populated) == len(chunks), (
        f"expected every chunk to have body_md populated; "
        f"only {len(populated)}/{len(chunks)} did"
    )


def test_body_md_is_markdown_with_headings() -> None:
    """F5: body_md should contain Markdown structure (heading markers)."""
    from fnd.extract import pdf

    chunks = list(pdf.extract(FIXTURE))
    md_with_heading = [c for c in chunks if c.body_md.lstrip().startswith("#")]
    assert md_with_heading, "expected at least one chunk's body_md to start with a Markdown heading"


def test_body_struct_still_populated_flat() -> None:
    """F1 invariant: even with extras installed, body_struct keeps its
    plain-text Block shape — the snippet pipeline reads from there and
    must not see Markdown markers."""
    from fnd.extract import pdf

    chunks = list(pdf.extract(FIXTURE))
    assert chunks
    for c in chunks:
        assert c.body_struct, "body_struct should still be populated"
        for block in c.body_struct:
            # No Markdown markers in body_struct — those go in body_md.
            assert (
                "**" not in block.text
            ), f"bold marker leaked into body_struct: {block.text[:80]!r}"
            assert not block.text.lstrip().startswith(
                "# "
            ), f"heading marker leaked into body_struct: {block.text[:80]!r}"
