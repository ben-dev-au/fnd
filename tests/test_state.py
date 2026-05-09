"""Phase B — UI state persistence."""

from __future__ import annotations

from pathlib import Path

import pytest

from acorn.state import UiState, load, save


def test_load_missing_file_returns_empty(tmp_path: Path) -> None:
    s = load(tmp_path / "nonexistent.toml")
    assert s == UiState()


def test_save_then_load_round_trips(tmp_path: Path) -> None:
    p = tmp_path / "scope.toml"
    original = UiState(
        collections=["DPC", "papers"],
        sources=["/Users/me/Notes", "/Users/me/Papers"],
        collapsed_panels=["filters"],
    )
    save(original, p)
    assert load(p) == original


def test_save_is_atomic(tmp_path: Path) -> None:
    """An interrupted write shouldn't leave a half-written scope.toml.
    We can't easily simulate a crash mid-rename, but we can confirm the
    intermediate ``.tmp`` file is gone after a successful write."""
    p = tmp_path / "scope.toml"
    save(UiState(collections=["a"]), p)
    tmp_marker = p.with_suffix(p.suffix + ".tmp")
    assert p.exists()
    assert not tmp_marker.exists()


def test_load_unreadable_file_returns_empty(tmp_path: Path) -> None:
    """Garbage on disk → quietly start fresh, don't crash the TUI."""
    p = tmp_path / "scope.toml"
    p.write_text("this is not [valid toml at all", encoding="utf-8")
    assert load(p) == UiState()


@pytest.mark.parametrize(
    "raw",
    [
        "[scope]\ncollections = []\nsources = []\n",
        "[scope]\n[panels]\n",
        "",
    ],
)
def test_load_partial_files_dont_crash(tmp_path: Path, raw: str) -> None:
    p = tmp_path / "scope.toml"
    p.write_text(raw, encoding="utf-8")
    assert load(p) == UiState()
