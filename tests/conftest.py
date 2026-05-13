"""Shared pytest fixtures."""

from __future__ import annotations

from collections.abc import Generator
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


@pytest.fixture(autouse=True)
def _quiet_preview_load_paths() -> Generator[None, None, None]:  # pyright: ignore[reportUnusedFunction]
    """Pin debounce + prefetch to 0 so cold-load assertions don't race
    the background worker. Pydantic v2 caches validators at class
    definition, so flipping ``model_fields[..].default`` needs
    ``model_rebuild(force=True)`` to take effect."""
    from acorn.config import Defaults

    debounce_field = Defaults.model_fields["preview_load_debounce_ms"]
    prefetch_field = Defaults.model_fields["preview_prefetch_count"]
    debounce_original = debounce_field.default
    prefetch_original = prefetch_field.default
    debounce_field.default = 0
    prefetch_field.default = 0
    Defaults.model_rebuild(force=True)
    try:
        yield
    finally:
        debounce_field.default = debounce_original
        prefetch_field.default = prefetch_original
        Defaults.model_rebuild(force=True)
