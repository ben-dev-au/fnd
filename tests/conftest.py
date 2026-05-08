"""Shared pytest fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(scope="session")
def fixtures_dir() -> Path:
    """Path to the small mixed-format test corpus."""
    return Path(__file__).parent / "fixtures"


@pytest.fixture
def tmp_index_dir(tmp_path: Path) -> Path:
    """Per-test isolated Tantivy index directory."""
    d = tmp_path / "index"
    d.mkdir()
    return d
