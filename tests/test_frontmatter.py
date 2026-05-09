"""Phase 5.5e-1: Obsidian-style YAML frontmatter parsing."""

from __future__ import annotations

from acorn.frontmatter import read_frontmatter_from_text


def test_no_frontmatter_returns_none() -> None:
    assert read_frontmatter_from_text("# Just a heading\nbody text\n") is None


def test_empty_frontmatter_block_returns_empty_dict() -> None:
    assert read_frontmatter_from_text("---\n---\nbody\n") == {}


def test_does_not_match_when_first_line_isnt_fence() -> None:
    """A leading blank line or any non-fence content disables frontmatter."""
    assert read_frontmatter_from_text("\n---\nfoo: bar\n---\n") is None
