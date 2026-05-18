"""Phase 5.5e-1: Obsidian-style YAML frontmatter parsing."""

from __future__ import annotations

import datetime as dt

import pytest

from fnd.frontmatter import FrontmatterParseError, read_frontmatter_from_text


def test_no_frontmatter_returns_none() -> None:
    assert read_frontmatter_from_text("# Just a heading\nbody text\n") is None


def test_empty_frontmatter_block_returns_empty_dict() -> None:
    assert read_frontmatter_from_text("---\n---\nbody\n") == {}


def test_does_not_match_when_first_line_isnt_fence() -> None:
    """A leading blank line or any non-fence content disables frontmatter."""
    assert read_frontmatter_from_text("\n---\nfoo: bar\n---\n") is None


def test_bare_scalar() -> None:
    out = read_frontmatter_from_text("---\nCourse: Design Patterns with C++\n---\nbody\n")
    assert out == {"Course": "Design Patterns with C++"}


def test_quoted_string_double() -> None:
    out = read_frontmatter_from_text('---\ntitle: "Final Draft"\n---\n')
    assert out == {"title": "Final Draft"}


def test_quoted_string_single() -> None:
    out = read_frontmatter_from_text("---\ntitle: 'Final Draft'\n---\n")
    assert out == {"title": "Final Draft"}


def test_integer_and_float() -> None:
    out = read_frontmatter_from_text("---\npriority: 3\nweight: 1.5\n---\n")
    assert out == {"priority": 3, "weight": 1.5}


def test_iso_date() -> None:
    out = read_frontmatter_from_text("---\ndue: 2026-06-01\n---\n")
    assert out == {"due": dt.date(2026, 6, 1)}


def test_bool_and_null() -> None:
    out = read_frontmatter_from_text(
        "---\narchived: false\nactive: true\nparent: null\nother: ~\n---\n"
    )
    assert out == {"archived": False, "active": True, "parent": None, "other": None}


def test_unsupported_nested_mapping_raises() -> None:
    with pytest.raises(FrontmatterParseError, match="nested"):
        read_frontmatter_from_text("---\nfoo:\n  bar: baz\n---\n")


def test_unsupported_anchor_raises() -> None:
    with pytest.raises(FrontmatterParseError, match=r"anchor|alias|unsupported"):
        read_frontmatter_from_text("---\nfoo: &x 1\nbar: *x\n---\n")


def test_invalid_line_no_colon_raises() -> None:
    with pytest.raises(FrontmatterParseError, match="line 2"):
        read_frontmatter_from_text("---\nbroken line no colon\n---\n")


def test_inline_list() -> None:
    out = read_frontmatter_from_text("---\ntags: [course, active, dpwc]\n---\n")
    assert out == {"tags": ["course", "active", "dpwc"]}


def test_inline_list_quoted_items() -> None:
    out = read_frontmatter_from_text("---\ntags: [\"with space\", 'one', plain]\n---\n")
    assert out == {"tags": ["with space", "one", "plain"]}


def test_inline_list_mixed_types() -> None:
    out = read_frontmatter_from_text("---\nvals: [1, 2.5, true, null]\n---\n")
    assert out == {"vals": [1, 2.5, True, None]}


def test_block_list() -> None:
    out = read_frontmatter_from_text("---\ntags:\n  - course\n  - active\n  - dpwc\n---\n")
    assert out == {"tags": ["course", "active", "dpwc"]}


def test_empty_inline_list() -> None:
    out = read_frontmatter_from_text("---\ntags: []\n---\n")
    assert out == {"tags": []}


def test_unterminated_inline_list_raises() -> None:
    with pytest.raises(FrontmatterParseError, match="list"):
        read_frontmatter_from_text("---\ntags: [course, active\n---\n")
