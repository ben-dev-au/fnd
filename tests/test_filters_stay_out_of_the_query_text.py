"""Filter clauses reach every pass as typed hard filters, never as query text.

Wrapped into the string as ``(kind:md) AND (the glimmer)``, they switched off
stopword stripping, so a chunk matching only "the" came back.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fnd.index import build_index
from fnd.layered import search_layered
from fnd.query import FilteredSearcher, Searcher


@pytest.fixture
def searcher(tmp_path: Path, tmp_index_dir: Path) -> Searcher:
    root = tmp_path / "corpus"
    root.mkdir()
    (root / "lamp.md").write_text("# Lamp\n\nthe glimmer of a lamp.\n")
    (root / "filler.md").write_text("# Filler\n\nthe one and the other of the two.\n")
    build_index(roots=[root], index_dir=tmp_index_dir, collection="c")
    return Searcher(index_dir=tmp_index_dir)


def _names_and_scores(searcher: object) -> dict[str, float]:
    groups = search_layered(searcher, query="the glimmer", limit=10)  # type: ignore[arg-type]
    return {Path(g.path).name: g.top_score for g in groups}


def test_a_stopword_only_chunk_stays_out_with_a_filter_on(searcher: Searcher) -> None:
    filtered = _names_and_scores(FilteredSearcher(searcher, clauses=["kind:md"]))
    assert set(filtered) == {"lamp.md"}


def test_a_filter_leaves_the_scores_alone(searcher: Searcher) -> None:
    unfiltered = _names_and_scores(searcher)
    filtered = _names_and_scores(FilteredSearcher(searcher, clauses=["kind:md"]))
    assert filtered == unfiltered


def test_a_clause_that_is_not_a_filter_is_refused(searcher: Searcher) -> None:
    with pytest.raises(ValueError, match="not a filter clause"):
        searcher._candidates("glimmer", window=10, collection=None, filter_clauses=["glimmer"])
