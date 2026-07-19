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
