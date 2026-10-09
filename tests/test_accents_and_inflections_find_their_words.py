"""Search and highlighting share one analyser (fnd.analysis): accents fold and
heading, title and path fields stem like the body."""

from __future__ import annotations

from pathlib import Path

import pytest

from fnd.analysis import analyse, index_token
from fnd.index import build_index
from fnd.layered import search_layered
from fnd.matching import MatchSpec, word_matches
from fnd.query import Searcher


@pytest.fixture
def searcher(tmp_path: Path, tmp_index_dir: Path) -> Searcher:
    root = tmp_path / "notes"
    root.mkdir()
    (root / "wine.md").write_text("# Crémant\n\nCrémant de Bourgogne from Mâcon.\n")
    (root / "algo.md").write_text("# Recursion\n\nA function that calls itself.\n")
    build_index(roots=[root], index_dir=tmp_index_dir, collection="c")
    return Searcher(index_dir=tmp_index_dir)


def _names(searcher: Searcher, query: str) -> set[str]:
    return {Path(g.path).name for g in search_layered(searcher, query=query, limit=10)}


@pytest.mark.parametrize("query", ["cremant", "macon", "crém*", "CREMANT"])
def test_an_unaccented_query_finds_the_accented_word(searcher: Searcher, query: str) -> None:
    assert _names(searcher, query) == {"wine.md"}


def test_an_inflected_heading_query_matches(searcher: Searcher) -> None:
    assert _names(searcher, "heading_path:recursions") == {"algo.md"}


@pytest.mark.parametrize("query", ["cremant", "crem*", "crém*"])
def test_highlighting_folds_the_same_way(query: str) -> None:
    assert word_matches("Crémant", MatchSpec.from_query(query))


def test_index_token_is_what_the_index_stores(searcher: Searcher) -> None:
    """Every word of an indexed body, run through index_token, is a term the index holds."""
    index = searcher._searcher
    for word in "Crémant de Bourgogne from Mâcon".split():
        assert index.doc_freq("body", index_token(word)) == 1, word
    assert analyse("Straße Œuvre") == ["strass", "oeuvr"]


@pytest.mark.parametrize("query", ["cremant", "crémant"])
def test_an_accent_paints_as_a_match_not_a_typo(query: str) -> None:
    from fnd.render import HIGHLIGHT_STYLE, word_highlight_runs

    runs = word_highlight_runs("Crémant", MatchSpec.from_query(query))
    assert runs == [(0, len("Crémant"), HIGHLIGHT_STYLE)]
