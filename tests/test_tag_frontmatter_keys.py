"""Custom frontmatter keys as tag sources: config, settings row, indexing."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from fnd.config import Config, Defaults, write_setting


def test_defaults_to_empty() -> None:
    assert Defaults().tag_frontmatter_keys == []


def test_accepts_a_list_of_keys() -> None:
    d = Defaults(tag_frontmatter_keys=["Course", "Notes_Type"])
    assert d.tag_frontmatter_keys == ["Course", "Notes_Type"]


def test_rejects_a_non_list() -> None:
    with pytest.raises(ValidationError):
        Defaults(tag_frontmatter_keys="Course")  # type: ignore[arg-type]


def test_round_trips_through_write_setting(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text("", encoding="utf-8")
    updated: Config = write_setting(
        config_path=cfg_path,
        dotted_path="defaults.tag_frontmatter_keys",
        value=["Course", "Topic"],
    )
    assert updated.defaults.tag_frontmatter_keys == ["Course", "Topic"]


def test_settings_row_parses_comma_text() -> None:
    from fnd.tui.menu import _coerce_str_list

    assert _coerce_str_list("Course, Notes_Type ,Topic") == ["Course", "Notes_Type", "Topic"]
    assert _coerce_str_list("") == []
    assert _coerce_str_list("  ,  ") == []


def test_custom_keys_reach_the_index(tmp_path: Path) -> None:
    """End to end: a configured key becomes a filterable namespaced tag."""
    from fnd.index import build_index
    from fnd.query import Searcher
    from fnd.tag_query import TagFilter

    root = tmp_path / "corpus"
    root.mkdir()
    (root / "wk1.md").write_text(
        '---\ntags: []\nCourse: "[[Design Patterns with C++]]"\n'
        "Notes_Type: [Study Notes]\n---\n\n# W\n\nsaffron\n",
        encoding="utf-8",
    )
    (root / "other.md").write_text(
        "---\nCourse: Algebra\n---\n\n# O\n\nsaffron\n", encoding="utf-8"
    )

    index_dir = tmp_path / "idx"
    build_index(
        roots=[root],
        index_dir=index_dir,
        collection="default",
        tag_frontmatter_keys=["Course", "Notes_Type"],
    )
    searcher = Searcher(index_dir=index_dir)

    def names(tag: str) -> set[str]:
        hits = searcher.search(
            "saffron", tag_filter=TagFilter(include={"frontmatter": frozenset({tag})})
        )
        return {Path(h.path).name for h in hits}

    # The wikilink brackets are stripped, and the value is namespaced.
    assert names("course/design patterns with c++") == {"wk1.md"}
    assert names("notes_type/study notes") == {"wk1.md"}
    # The namespace itself selects everything under it.
    assert names("course") == {"wk1.md", "other.md"}
    # Empty tags: contributes nothing.
    assert names("") == set()


def test_custom_keys_absent_by_default(tmp_path: Path) -> None:
    """Without config, custom keys must not silently become tags."""
    from fnd.index import build_index
    from fnd.query import Searcher
    from fnd.tag_query import TagFilter

    root = tmp_path / "corpus"
    root.mkdir()
    (root / "a.md").write_text("---\nCourse: Algebra\n---\n\n# A\n\nsaffron\n", encoding="utf-8")
    index_dir = tmp_path / "idx"
    build_index(roots=[root], index_dir=index_dir, collection="default")
    searcher = Searcher(index_dir=index_dir)
    hits = searcher.search(
        "saffron", tag_filter=TagFilter(include={"frontmatter": frozenset({"course"})})
    )
    assert hits == []
