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


@pytest.fixture(autouse=True)
def isolated_ui_state(  # pyright: ignore[reportUnusedFunction]
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Redirect the persistent UI state file at a per-test temp path so
    a test's scope-toggle doesn't pollute other tests (or the user's
    real ``scope.toml``)."""
    p = tmp_path / "ui_state" / "scope.toml"
    monkeypatch.setattr("acorn.state._state_path", lambda: p)
    return p
