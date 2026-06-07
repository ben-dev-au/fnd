"""Unquoted queries must not return stopword-only matches.

A chunk that overlaps the query only via a stopword ("and"/"in"/"the")
carries ~zero IDF — it ranks at the tail but was still retrieved, pure
noise (and, since stopword highlighting was dropped, shows no highlight).
A chunk must match at least one CONTENT term to be returned. Quoted
phrases keep their stopwords.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fnd.index import build_index
from fnd.query import Searcher


@pytest.fixture
def stopword_index(tmp_path: Path, tmp_index_dir: Path) -> Path:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "content.md").write_text(
        "# Defence\n\nMonitoring segmentation and defence in depth strategy.\n"
    )
    (corpus / "stopword.md").write_text("# Kitchen\n\nRecipes in the pantry and the kitchen.\n")
    (corpus / "phrase.md").write_text(
        "# Approach\n\nOur defence in depth posture across the network.\n"
    )
    build_index(roots=[corpus], index_dir=tmp_index_dir, collection="default")
    return tmp_index_dir


def _paths(hits: list) -> list[str]:  # type: ignore[type-arg]
    return [h.path for h in hits]


def test_unquoted_excludes_stopword_only_chunk(stopword_index: Path) -> None:
    hits = Searcher(index_dir=stopword_index).search(
        "monitoring segmentation and defence in depth", limit=10
    )
    paths = _paths(hits)
    assert any(p.endswith("content.md") for p in paths), "content chunk must be returned"
    assert not any(p.endswith("stopword.md") for p in paths), (
        "stopword-only chunk must NOT be returned"
    )


def test_quoted_phrase_keeps_stopwords(stopword_index: Path) -> None:
    hits = Searcher(index_dir=stopword_index).search('"defence in depth"', limit=10)
    paths = _paths(hits)
    assert any(p.endswith("phrase.md") for p in paths), (
        'quoted phrase "defence in depth" must still match (needs "in")'
    )


def test_all_stopword_query_does_not_crash(stopword_index: Path) -> None:
    # Entirely-stopword query falls back to original behaviour rather than
    # becoming empty / crashing.
    hits = Searcher(index_dir=stopword_index).search("the and in", limit=10)
    assert isinstance(hits, list)
