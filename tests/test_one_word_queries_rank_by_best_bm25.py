"""A one-word query ranks by each hit's best BM25: with no phrase pass to weigh,
fusion only lets the literal word's noise outrank its expansions."""

from __future__ import annotations

from pathlib import Path

from fnd.index import build_index
from fnd.layered import search_layered
from fnd.query import Searcher
from fnd.synonyms import SynonymTable


def test_an_acronym_ranks_its_expansion_by_strength(tmp_path: Path, tmp_index_dir: Path) -> None:
    root = tmp_path / "notes"
    root.mkdir()
    filler = " ".join(f"word{i}" for i in range(120))
    (root / "a_literal.md").write_text(f"# Config\n\n{filler} sso {filler}\n")
    (root / "b_topic.md").write_text(
        "# Single sign-on\n\nsingle sign-on lets one login reach many apps; single sign-on.\n"
    )
    build_index(roots=[root], index_dir=tmp_index_dir, collection="c")
    table = SynonymTable.from_groups([["sso", "single sign-on"]])

    groups = search_layered(
        Searcher(index_dir=tmp_index_dir), query="sso", limit=10, synonyms=table
    )
    assert [Path(g.path).name for g in groups] == ["b_topic.md", "a_literal.md"]
    assert groups[0].top_score >= groups[1].top_score


def test_an_exact_match_stays_above_fuzzy_near_misses(tmp_path: Path, tmp_index_dir: Path) -> None:
    """When the cascade answers, its exact hits keep their place above its fuzzy ones."""
    root = tmp_path / "notes"
    root.mkdir()
    filler = " ".join(f"word{i}" for i in range(300))
    (root / "exact.md").write_text(f"# Farm\n\n{filler} lamb {filler}\n")
    (root / "near.md").write_text("# Light\n\nlamp lamp lamp\n")
    build_index(roots=[root], index_dir=tmp_index_dir, collection="c")

    groups = search_layered(Searcher(index_dir=tmp_index_dir), query="lamb", limit=50)
    assert [Path(g.path).name for g in groups] == ["exact.md", "near.md"]
