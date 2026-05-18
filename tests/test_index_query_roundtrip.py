"""Acceptance test for Phase 1: index the fixture corpus, query for the unique anchor
phrases, assert the right (path, locator) tuple is rank #1.

Per §15: this test is the contract for Phase 1. It MUST go red before any extractor /
indexer code exists, then green once tasks #4-#6 land.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fnd.index import build_index
from fnd.query import Searcher


@pytest.fixture
def built_index(fixtures_dir: Path, tmp_index_dir: Path) -> Path:
    build_index(roots=[fixtures_dir], index_dir=tmp_index_dir, collection="default")
    return tmp_index_dir


def test_pdf_anchor_attributes_to_correct_page(built_index: Path) -> None:
    hits = Searcher(index_dir=built_index).search("blue penguin sandwich", limit=5)

    assert hits, "expected at least one hit for the unique anchor phrase"
    top = hits[0]
    assert top.path.endswith("papers/test.pdf"), f"top hit was {top.path}"
    assert top.page == 7, f"expected page 7, got {top.page}"


def test_md_anchor_attributes_to_correct_section(built_index: Path) -> None:
    hits = Searcher(index_dir=built_index).search("ostrich firewall", limit=5)

    assert hits
    top = hits[0]
    assert top.path.endswith("notes/index.md")
    assert "Sampling" in top.heading_path, f"expected Sampling in {top.heading_path!r}"


def test_txt_anchor_attributes_to_correct_file(built_index: Path) -> None:
    hits = Searcher(index_dir=built_index).search("marigold compiler", limit=5)

    assert hits
    top = hits[0]
    assert top.path.endswith("plain/short.txt")


def test_unrelated_query_does_not_return_anchor(built_index: Path) -> None:
    """Sanity: anchors should NOT surface for unrelated queries."""
    hits = Searcher(index_dir=built_index).search("nonexistent zebra unicorn", limit=5)
    assert not hits or all("blue penguin sandwich" not in h.snippet for h in hits)
