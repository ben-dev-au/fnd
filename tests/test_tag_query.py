"""Tag filters compile to typed queries, never to interpolated strings."""

from __future__ import annotations

from pathlib import Path

import tantivy

from fnd.schema import F_TAGS_FM, F_TAGS_OS, build_schema
from fnd.tag_query import TagFilter, compile_tag_filter

HOSTILE = 'evil" OR body:classified OR "'


def _build(tmp_path: Path) -> tantivy.Index:
    index = tantivy.Index(build_schema(), path=str(tmp_path))
    w = index.writer(15_000_000)
    rows = [
        ("normal.md", "public notes", ["recipe", "project", "project/alpha"], ["red"]),
        ("spaced.md", "public notes", ["two words"], []),
        ("hostile.md", "public notes", [HOSTILE], []),
        ("secret.md", "classified dossier", ["private"], []),
        ("both.md", "public notes", ["recipe"], ["recipe"]),
    ]
    for pid, body, fm, os_ in rows:
        d = tantivy.Document()
        d.add_text("parent_id", pid)
        d.add_text("body", body)
        for t in fm:
            d.add_text(F_TAGS_FM, t)
        for t in os_:
            d.add_text(F_TAGS_OS, t)
        w.add_document(d)
    w.commit()
    index.reload()
    return index


def _files(index: tantivy.Index, q: tantivy.Query) -> set[str]:
    s = index.searcher()
    return {str(s.doc(a).get_first("parent_id")) for _, a in s.search(q, 50).hits}


def test_hostile_tag_matches_only_itself(tmp_path: Path) -> None:
    """Injection regression: interpolating this value into a query string
    returns documents that don't carry the tag at all."""
    index = _build(tmp_path)
    q = compile_tag_filter(TagFilter(include={"frontmatter": frozenset({HOSTILE})}), build_schema())
    assert q is not None
    assert _files(index, q) == {"hostile.md"}


def test_values_with_spaces_need_no_quoting(tmp_path: Path) -> None:
    index = _build(tmp_path)
    q = compile_tag_filter(
        TagFilter(include={"frontmatter": frozenset({"two words"})}), build_schema()
    )
    assert q is not None
    assert _files(index, q) == {"spaced.md"}


def test_match_all_requires_every_tag(tmp_path: Path) -> None:
    index = _build(tmp_path)
    q = compile_tag_filter(
        TagFilter(include={"frontmatter": frozenset({"recipe", "project/alpha"})}, match_all=True),
        build_schema(),
    )
    assert q is not None
    assert _files(index, q) == {"normal.md"}


def test_match_any_unions_tags(tmp_path: Path) -> None:
    index = _build(tmp_path)
    q = compile_tag_filter(
        TagFilter(include={"frontmatter": frozenset({"two words", "private"})}, match_all=False),
        build_schema(),
    )
    assert q is not None
    assert _files(index, q) == {"spaced.md", "secret.md"}


def test_exclude_subtracts(tmp_path: Path) -> None:
    index = _build(tmp_path)
    q = compile_tag_filter(
        TagFilter(exclude={"frontmatter": frozenset({"private"})}), build_schema()
    )
    assert q is not None
    got = _files(index, q)
    assert "secret.md" not in got
    assert "normal.md" in got


def test_exclude_applies_regardless_of_match_mode(tmp_path: Path) -> None:
    index = _build(tmp_path)
    for match_all in (True, False):
        q = compile_tag_filter(
            TagFilter(
                include={"frontmatter": frozenset({"recipe"})},
                exclude={"frontmatter": frozenset({"project"})},
                match_all=match_all,
            ),
            build_schema(),
        )
        assert q is not None
        assert _files(index, q) == {"both.md"}


def test_sources_are_independent(tmp_path: Path) -> None:
    """'recipe' as an OS tag must not match a file tagged only in frontmatter."""
    index = _build(tmp_path)
    q = compile_tag_filter(TagFilter(include={"os": frozenset({"recipe"})}), build_schema())
    assert q is not None
    assert _files(index, q) == {"both.md"}


def test_nested_parent_matches_descendants(tmp_path: Path) -> None:
    """Ancestors were expanded at index time, so this is a plain term match."""
    index = _build(tmp_path)
    q = compile_tag_filter(
        TagFilter(include={"frontmatter": frozenset({"project"})}), build_schema()
    )
    assert q is not None
    assert _files(index, q) == {"normal.md"}


def test_empty_filter_compiles_to_none() -> None:
    assert compile_tag_filter(TagFilter(), build_schema()) is None


def test_unknown_source_is_ignored() -> None:
    spec = TagFilter(include={"telepathy": frozenset({"x"})})
    assert compile_tag_filter(spec, build_schema()) is None


def test_is_empty_reports_no_selection() -> None:
    assert TagFilter().is_empty()
    assert TagFilter(include={"frontmatter": frozenset()}).is_empty()
    assert not TagFilter(include={"frontmatter": frozenset({"a"})}).is_empty()
