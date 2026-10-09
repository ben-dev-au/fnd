"""Automatic fuzzy matching never treats a number as a typo, in search or paint;
an explicit ``~N`` still does."""

from __future__ import annotations

from pathlib import Path

from fnd.cascade import cascade_search
from fnd.index import build_index
from fnd.matching import MatchSpec, word_matches
from fnd.query import Searcher


def test_a_year_does_not_find_the_neighbouring_year(tmp_path: Path, tmp_index_dir: Path) -> None:
    root = tmp_path / "notes"
    root.mkdir()
    (root / "older.md").write_text("# Report\n\nthe 2025 threat report.\n")
    build_index(roots=[root], index_dir=tmp_index_dir, collection="c")
    searcher = Searcher(index_dir=tmp_index_dir)
    assert cascade_search(searcher, query="2026", threshold=50, limit=50) == []
    assert cascade_search(searcher, query="2026~1", threshold=50, limit=50), (
        "explicit ~N still fuzzes"
    )


def test_a_number_is_painted_only_where_it_occurs() -> None:
    assert not word_matches("2025", MatchSpec.from_query("2026"))
    assert word_matches("2025", MatchSpec.from_query("2026~1"))
    assert word_matches("algorithm", MatchSpec.from_query("algoritm")), "words still auto-fuzz"
