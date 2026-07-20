"""Normalisation, ancestor expansion, and the frontmatter provider."""

from __future__ import annotations

from pathlib import Path

import pytest

from fnd.tags import (
    MAX_TAGS_PER_FILE,
    FrontmatterTagProvider,
    TagContext,
    expand_ancestors,
    normalise_tag,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("recipe", "recipe"),
        ("#recipe", "recipe"),
        ("  Recipe  ", "recipe"),
        ("Project/Alpha", "project/alpha"),
        ("two   words", "two words"),
        ("", ""),
        ("#", ""),
        ("   ", ""),
    ],
)
def test_normalise_tag(raw: str, expected: str) -> None:
    assert normalise_tag(raw) == expected


def test_normalise_truncates_absurd_tags() -> None:
    assert len(normalise_tag("x" * 500)) <= 128


@pytest.mark.parametrize(
    ("tag", "expected"),
    [
        ("recipe", {"recipe"}),
        ("a/b", {"a", "a/b"}),
        ("a/b/c", {"a", "a/b", "a/b/c"}),
        ("a//b", {"a", "a/b"}),
        ("/leading", {"leading"}),
    ],
)
def test_expand_ancestors(tag: str, expected: set[str]) -> None:
    assert expand_ancestors(tag) == expected


def _ctx(fm: dict[str, object] | None) -> TagContext:
    return TagContext(path=Path("/tmp/x.md"), frontmatter=fm)


def test_reads_list_form() -> None:
    got = FrontmatterTagProvider().read(_ctx({"tags": ["recipe", "Dinner"]}))
    assert got == frozenset({"recipe", "dinner"})


def test_reads_singular_tag_key() -> None:
    assert FrontmatterTagProvider().read(_ctx({"tag": "recipe"})) == frozenset({"recipe"})


def test_expands_nested_tags() -> None:
    got = FrontmatterTagProvider().read(_ctx({"tags": ["project/alpha"]}))
    assert got == frozenset({"project", "project/alpha"})


def test_siblings_share_one_ancestor() -> None:
    got = FrontmatterTagProvider().read(_ctx({"tags": ["p/alpha", "p/beta"]}))
    assert got == frozenset({"p", "p/alpha", "p/beta"})


@pytest.mark.parametrize("fm", [None, {}, {"title": "no tags here"}])
def test_absent_tags_yield_empty(fm: dict[str, object] | None) -> None:
    assert FrontmatterTagProvider().read(_ctx(fm)) == frozenset()


def test_non_string_values_are_skipped_not_fatal() -> None:
    got = FrontmatterTagProvider().read(_ctx({"tags": ["ok", None, {"a": 1}, 42]}))
    assert "ok" in got
    assert "42" in got


def test_tag_count_is_bounded() -> None:
    got = FrontmatterTagProvider().read(_ctx({"tags": [f"t{i}" for i in range(5000)]}))
    assert len(got) <= MAX_TAGS_PER_FILE


def test_provider_is_available_on_every_platform() -> None:
    p = FrontmatterTagProvider()
    assert p.available_on("darwin")
    assert p.available_on("win32")
    assert p.available_on("linux")


def test_comma_separated_string_form_splits() -> None:
    """Obsidian's inline form `tags: a, b` is two tags, not one literal.

    Found against a real vault: `tags: cheatsheets, python` was stored as the
    single tag "cheatsheets, python", so neither name matched.
    """
    got = FrontmatterTagProvider().read(_ctx({"tags": "cheatsheets, python"}))
    assert got == frozenset({"cheatsheets", "python"})


def test_comma_form_tolerates_padding_and_empties() -> None:
    got = FrontmatterTagProvider().read(_ctx({"tags": " a ,, b , "}))
    assert got == frozenset({"a", "b"})


def test_comma_form_inside_a_list_item_splits_too() -> None:
    got = FrontmatterTagProvider().read(_ctx({"tags": ["a, b", "c"]}))
    assert got == frozenset({"a", "b", "c"})


def test_single_string_without_comma_is_one_tag() -> None:
    assert FrontmatterTagProvider().read(_ctx({"tags": "recipe"})) == frozenset({"recipe"})


def test_comma_form_expands_ancestors() -> None:
    got = FrontmatterTagProvider().read(_ctx({"tags": "project/alpha, solo"}))
    assert got == frozenset({"project", "project/alpha", "solo"})


# ── custom frontmatter keys as tag sources ────────────────────────────


def test_custom_keys_are_ignored_by_default() -> None:
    """Only tags:/tag: unless the user opts a key in."""
    got = FrontmatterTagProvider().read(_ctx({"Course": "Design Patterns"}))
    assert got == frozenset()


def test_custom_key_values_become_namespaced_tags() -> None:
    """A vault's real taxonomy often lives in custom keys. Namespacing by key
    keeps them grouped in the pane and avoids colliding with tags: values."""
    provider = FrontmatterTagProvider(extra_keys=["Course", "Notes_Type"])
    got = provider.read(_ctx({"Course": "Design Patterns", "Notes_Type": ["Assignment"]}))
    assert got == frozenset(
        {"course", "course/design patterns", "notes_type", "notes_type/assignment"}
    )


def test_custom_key_strips_obsidian_wikilinks() -> None:
    """Obsidian writes `Course: "[[Design Patterns with C++]]"`."""
    provider = FrontmatterTagProvider(extra_keys=["Course"])
    got = provider.read(_ctx({"Course": "[[Design Patterns with C++]]"}))
    assert "course/design patterns with c++" in got


def test_custom_key_matching_is_case_insensitive() -> None:
    provider = FrontmatterTagProvider(extra_keys=["course"])
    assert "course/algebra" in provider.read(_ctx({"Course": "Algebra"}))


def test_custom_key_handles_lists_and_commas() -> None:
    provider = FrontmatterTagProvider(extra_keys=["Topic"])
    got = provider.read(_ctx({"Topic": ["Trees, Graphs", "Sorting"]}))
    assert got == frozenset({"topic", "topic/trees", "topic/graphs", "topic/sorting"})


def test_empty_custom_key_contributes_nothing() -> None:
    provider = FrontmatterTagProvider(extra_keys=["Topic"])
    assert provider.read(_ctx({"Topic": [], "Module": "x"})) == frozenset()


def test_custom_keys_coexist_with_plain_tags() -> None:
    provider = FrontmatterTagProvider(extra_keys=["Course"])
    got = provider.read(_ctx({"tags": ["exam"], "Course": "DPC"}))
    assert "exam" in got
    assert "course/dpc" in got


def test_tags_key_cannot_be_double_counted_as_a_custom_key() -> None:
    """Naming 'tags' as an extra key must not namespace the real tags."""
    provider = FrontmatterTagProvider(extra_keys=["tags"])
    assert provider.read(_ctx({"tags": ["exam"]})) == frozenset({"exam"})
