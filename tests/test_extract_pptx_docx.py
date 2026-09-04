"""Acceptance: PPTX and DOCX anchors round-trip with structure."""

from __future__ import annotations

from pathlib import Path

import pytest

from fnd.index import build_index
from fnd.query import Searcher


@pytest.fixture
def built_index(fixtures_dir: Path, tmp_index_dir: Path) -> Path:
    build_index(roots=[fixtures_dir], index_dir=tmp_index_dir, collection="default")
    return tmp_index_dir


def test_pptx_anchor_attributes_to_correct_slide(built_index: Path) -> None:
    hits = Searcher(index_dir=built_index).search("lavender stapler", limit=5)

    assert hits, "expected at least one hit"
    top = hits[0]
    assert Path(top.path).as_posix().endswith("slides/deck.pptx")
    assert top.slide == 4, f"expected slide 4, got {top.slide}"


def test_docx_anchor_attributes_to_correct_section(built_index: Path) -> None:
    hits = Searcher(index_dir=built_index).search("narwhal compiler", limit=5)

    assert hits
    top = hits[0]
    assert Path(top.path).as_posix().endswith("docs/methods.docx")
    assert "Sampling" in top.heading_path, f"got heading_path={top.heading_path!r}"


def test_pptx_speaker_notes_are_indexed(built_index: Path) -> None:
    """Speaker notes should be searchable so a reminder phrase can surface a slide."""
    hits = Searcher(index_dir=built_index).search("differentiating tool", limit=5)
    assert hits
    top = hits[0]
    assert Path(top.path).as_posix().endswith("slides/deck.pptx")
