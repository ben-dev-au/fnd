"""A compound word finds its hyphenated form, and the reverse.

The tokenizer splits ``drop-down`` into two words, so ``dropdown`` can reach it
neither by spelling nor by fuzz (four edits from either half), and the preview
highlighted nothing where the search had matched.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fnd.index import build_index
from fnd.layered import search_layered
from fnd.matching import MatchSpec
from fnd.query import Searcher
from fnd.render import match_word_spans, text_has_any_match
from fnd.synonyms import compound_table


@pytest.fixture
def index(tmp_path: Path, tmp_index_dir: Path) -> Path:
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "hyphen.md").write_text("## Menus\n\nUse a drop-down filter here.\n")
    (notes / "joined.md").write_text("## Lists\n\nA dropdown list lives here.\n")
    (notes / "word.md").write_text("## Word\n\nA notable result.\n")
    (notes / "split.md").write_text("## Split\n\nIt is not able to run.\n")
    build_index(roots=[notes], index_dir=tmp_index_dir, collection="notes")
    return tmp_index_dir


def _files(index: Path, query: str) -> set[str]:
    groups, _ = search_layered(
        Searcher(index_dir=index), query=query, limit=10, collection="notes", with_trace=True
    )
    return {Path(g.path).name for g in groups}


def test_a_joined_word_finds_its_hyphenated_form(index: Path) -> None:
    assert "hyphen.md" in _files(index, "dropdown")


def test_a_hyphenated_word_finds_its_joined_form(index: Path) -> None:
    assert "joined.md" in _files(index, "drop-down")


def test_a_split_widens_a_search_and_never_narrows_it(index: Path) -> None:
    """The exact hit stays; a split that is another phrase only adds to it."""
    assert "word.md" in _files(index, "notable")


def test_short_words_and_quoted_phrases_are_not_split() -> None:
    assert compound_table('into "dropdown menu"').groups == ()


def test_field_qualifiers_and_wildcards_are_left_alone() -> None:
    assert compound_table("title:dropdown dropdo*").groups == ()


def test_the_hyphenated_form_highlights_as_one_match() -> None:
    text = "Use a drop-down filter"
    runs = match_word_spans(text, MatchSpec.from_query("dropdown"))
    assert [text[a:b] for a, b, _ in runs] == ["drop-down"]


def test_the_joined_form_highlights_for_a_hyphenated_query() -> None:
    text = "A dropdown list"
    runs = match_word_spans(text, MatchSpec.from_query("drop-down"))
    assert [text[a:b] for a, b, _ in runs] == ["dropdown"]


def test_the_hyphenated_form_counts_as_a_match_the_user_can_see() -> None:
    """Every surface that claims a match (row marker, scrollbar, n/b) asks this."""
    assert text_has_any_match("Use a drop-down filter", MatchSpec.from_query("dropdown"))


@pytest.fixture
def crowded(tmp_path: Path, tmp_index_dir: Path) -> Path:
    """Enough literal hits either way that the search fuses rather than cascades."""
    notes = tmp_path / "crowded"
    notes.mkdir()
    for i in range(4):
        (notes / f"joined{i}.md").write_text(f"## Joined {i}\n\nA dropdown list {i}.\n")
        (notes / f"hyphen{i}.md").write_text(f"## Hyphen {i}\n\nA drop-down menu {i}.\n")
    build_index(roots=[notes], index_dir=tmp_index_dir, collection="notes")
    return tmp_index_dir


@pytest.mark.parametrize(("query", "other"), [("dropdown", "hyphen"), ("drop-down", "joined")])
def test_literal_hits_do_not_hide_the_other_spelling(crowded: Path, query: str, other: str) -> None:
    groups, trace = search_layered(
        Searcher(index_dir=crowded), query=query, limit=10, collection="notes", with_trace=True
    )
    assert trace.regime == "fusion", "the fixture no longer reaches the fusion regime"
    assert sum(Path(g.path).name.startswith(other) for g in groups) == 4


@pytest.fixture
def one_strong(tmp_path: Path, tmp_index_dir: Path) -> Path:
    """One file that matches the joined word outright, so the search takes its shortcut."""
    notes = tmp_path / "strong"
    notes.mkdir()
    (notes / "joined.md").write_text("## Dropdown\n\nDropdown, dropdown, dropdown.\n")
    for i in range(3):
        (notes / f"hyphen{i}.md").write_text(f"## Hyphen {i}\n\nA drop-down menu {i}.\n")
    build_index(roots=[notes], index_dir=tmp_index_dir, collection="notes")
    return tmp_index_dir


def test_a_lone_strong_match_does_not_hide_the_other_spelling(one_strong: Path) -> None:
    groups, trace = search_layered(
        Searcher(index_dir=one_strong),
        query="dropdown",
        limit=10,
        collection="notes",
        with_trace=True,
    )
    assert trace.regime == "strong-signal", "the fixture no longer takes the shortcut"
    names = [Path(g.path).name for g in groups]
    assert names[0] == "joined.md", "the outright match must still lead"
    assert sum(n.startswith("hyphen") for n in names) == 3
