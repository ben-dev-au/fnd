"""Phase 2 acceptance: per-section ranking within files."""

from __future__ import annotations

from pathlib import Path

import pytest

from fnd.index import build_index
from fnd.query import Searcher


@pytest.fixture
def built_index(fixtures_dir: Path, tmp_index_dir: Path) -> Path:
    build_index(roots=[fixtures_dir], index_dir=tmp_index_dir, collection="default")
    return tmp_index_dir


def test_search_grouped_returns_multiple_sections_for_one_file(built_index: Path) -> None:
    """A query matching multiple sections of one file should return all of them
    grouped under that file, ranked by score."""
    s = Searcher(index_dir=built_index)
    # "page" appears on every page of test.pdf (the "Page N" header).
    groups = s.search_grouped("page", limit=10, sections_per_file=10)

    pdf_groups = [g for g in groups if g.path.endswith("test.pdf")]
    assert pdf_groups, "expected a group for test.pdf"
    pdf = pdf_groups[0]
    assert len(pdf.hits) >= 5, f"expected several pages grouped, got {len(pdf.hits)}"
    # Pages should each have a distinct page number.
    pages = [h.page for h in pdf.hits]
    assert len(set(pages)) == len(pages), f"pages not distinct: {pages}"


def test_search_dedups_to_one_per_file(built_index: Path) -> None:
    """Default `search()` returns one hit per file, keeping the legacy contract."""
    s = Searcher(index_dir=built_index)
    hits = s.search("page", limit=10)
    parent_ids = [h.parent_id for h in hits]
    assert len(parent_ids) == len(set(parent_ids)), "expected one hit per file"


def test_grouped_top_score_matches_first_hit(built_index: Path) -> None:
    """FileGroup.top_score should always equal hits[0].score."""
    s = Searcher(index_dir=built_index)
    groups = s.search_grouped("page", limit=5)
    for g in groups:
        assert g.top_score == g.hits[0].score
