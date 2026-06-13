"""Regression: a ``{N}`` proximity query on a phrase that has a synonym used to
crash with "invalid query syntax".

Fusion's synonym pass rewrote ``{20}threat intelligence`` into
``{20}("threat intelligence" OR ti)`` (the bundled defaults map ``ti`` ↔
``threat intelligence``); the orphan ``{20}`` brace then reached Tantivy and was
rejected. The cascade fallback's synonym pass had the same unguarded shape.
"""

from __future__ import annotations

from pathlib import Path

from fnd.cascade import cascade_search
from fnd.index import build_index
from fnd.layered import search_layered
from fnd.query import Searcher
from fnd.synonyms import load_default_synonyms


def _corpus(tmp_path: Path, index_dir: Path) -> Searcher:
    root = tmp_path / "docs"
    root.mkdir(parents=True)
    # Several docs with the phrase adjacent so the proximity lex pass returns
    # enough hits to keep fusion out of the cascade fallback, and the top-2
    # scores sit close enough that strong-signal bypass never fires.
    for i in range(4):
        (root / f"d{i}.md").write_text(
            f"# D{i}\nThreat intelligence feeds inform the SOC about threat "
            f"intelligence trends and indicators.\n",
            encoding="utf-8",
        )
    build_index(roots=[tmp_path], index_dir=index_dir, collection="default")
    return Searcher(index_dir=index_dir)


def test_default_table_has_threat_intelligence_synonym() -> None:
    table = load_default_synonyms()
    assert table.expansions_for("threat intelligence") is not None


def test_search_layered_proximity_with_synonym_does_not_crash(
    tmp_path: Path, tmp_index_dir: Path
) -> None:
    s = _corpus(tmp_path, tmp_index_dir)
    groups = search_layered(
        s, query="{20}threat intelligence", limit=10, synonyms=load_default_synonyms()
    )
    assert groups, "proximity-on-synonymed-phrase must return results, not raise"


def test_cascade_proximity_with_synonym_does_not_crash(tmp_path: Path, tmp_index_dir: Path) -> None:
    """The cascade fallback's synonym pass had the same unguarded expansion. A
    high threshold forces the synonym pass to run; it must stand down for the
    precision-bearing proximity query instead of crashing."""
    s = _corpus(tmp_path, tmp_index_dir)
    hits = cascade_search(
        s,
        query="{20}threat intelligence",
        threshold=1000,
        limit=10,
        synonyms=load_default_synonyms(),
    )
    assert hits, "cascade must return the literal-pass hits, not raise"
