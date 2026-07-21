"""Frontmatter is metadata, not prose, and must not reach the body."""

from __future__ import annotations

from pathlib import Path

from fnd.extract.markdown import extract

DOC = """---
title: My Note
tags: [recipe, dinner]
---

# Real Heading

Real body text.
"""


def test_frontmatter_keys_are_not_in_body(tmp_path: Path) -> None:
    f = tmp_path / "a.md"
    f.write_text(DOC, encoding="utf-8")
    body = " ".join(c.body for c in extract(f))
    assert "recipe" not in body
    assert "My Note" not in body
    assert "Real body text." in body


def test_frontmatter_is_not_mistaken_for_a_heading(tmp_path: Path) -> None:
    f = tmp_path / "a.md"
    f.write_text(DOC, encoding="utf-8")
    headings = {c.heading_path for c in extract(f)}
    assert not any("title" in h for h in headings)
    assert any("Real Heading" in h for h in headings)


def test_line_numbers_survive_the_strip(tmp_path: Path) -> None:
    """F_LINE drives deep links; blanking must preserve 1-based line numbers."""
    f = tmp_path / "a.md"
    f.write_text(DOC, encoding="utf-8")
    chunks = [c for c in extract(f) if "Real Heading" in c.heading_path]
    assert chunks
    assert chunks[0].line == 6  # 1-based line of "# Real Heading"


def test_document_without_frontmatter_is_untouched(tmp_path: Path) -> None:
    f = tmp_path / "b.md"
    f.write_text("# Title\n\nBody.\n", encoding="utf-8")
    chunks = list(extract(f))
    assert "Body." in " ".join(c.body for c in chunks)
    assert chunks[0].line == 1


def test_horizontal_rule_is_not_treated_as_frontmatter(tmp_path: Path) -> None:
    """A --- rule mid-document must survive."""
    f = tmp_path / "c.md"
    f.write_text("# Title\n\nBefore.\n\n---\n\nAfter.\n", encoding="utf-8")
    body = " ".join(c.body for c in extract(f))
    assert "Before." in body
    assert "After." in body


def test_unterminated_fence_is_left_alone(tmp_path: Path) -> None:
    """No closing ---: treat the whole thing as content rather than eating it."""
    f = tmp_path / "d.md"
    f.write_text("---\ntitle: dangling\n\n# Heading\n\nBody.\n", encoding="utf-8")
    body = " ".join(c.body for c in extract(f))
    assert "Body." in body


def test_crlf_line_endings_not_mixed(tmp_path: Path) -> None:
    """Blanking CRLF frontmatter must not leave the region with bare \\n
    while the body keeps CRLF."""
    f = tmp_path / "crlf.md"
    f.write_bytes(b"---\r\ntags: [recipe]\r\n---\r\n\r\n# Heading\r\n\r\nBody text.\r\n")
    chunks = list(extract(f))
    body = " ".join(c.body for c in chunks)
    assert "recipe" not in body
    assert "Body text." in body
    assert any("Heading" in c.heading_path for c in chunks)
