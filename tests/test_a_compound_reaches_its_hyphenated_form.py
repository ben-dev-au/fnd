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
