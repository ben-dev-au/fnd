"""Shared pytest fixtures."""

from __future__ import annotations

import importlib.util
from collections.abc import Generator
from pathlib import Path

import pytest


# When the pdf-structure extra is installed in the dev venv, PDF chunks
# carry body_md and the preview dispatcher routes them through the
# structural Markdown widget. Most existing tests pre-date that and
# assert the flat-buffer routing PDFs have always taken — they pass
# in CI (no extra installed) but fail locally when a dev has installed
# it. Default the whole test suite to flat-PDF routing so the
# invariant tests stay green; the two structural-PDF tests opt out
# explicitly via `monkeypatch.delenv("_FND_FORCE_FLAT", raising=False)`.
def _pdf_structure_actually_works() -> bool:
    """``find_spec`` returns True even when pymupdf4llm has been
    half-uninstalled (namespace dir survives, ``to_markdown`` gone).
    Verify the entrypoint actually exists before claiming the extra
    is installed."""
    spec = importlib.util.find_spec("pymupdf4llm")
    if spec is None:
        return False
    try:
        import pymupdf4llm
    except Exception:
        return False
    return hasattr(pymupdf4llm, "to_markdown")


_PDF_STRUCTURE_INSTALLED = _pdf_structure_actually_works()


@pytest.fixture(autouse=True)
def _default_pdf_flat_when_extras_present(  # pyright: ignore[reportUnusedFunction]
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if _PDF_STRUCTURE_INSTALLED:
        # "pdf" forces PDF-only to the flat path; MD/DOCX/PPTX are
        # unaffected. Tests asserting structural PDF routing override
        # via `monkeypatch.delenv("_FND_FORCE_FLAT", raising=False)`.
        monkeypatch.setenv("_FND_FORCE_FLAT", "pdf")


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
    monkeypatch.setattr("fnd.state._state_path", lambda: p)
    return p


@pytest.fixture(autouse=True)
def isolated_pdf_structure_cache(  # pyright: ignore[reportUnusedFunction]
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Point fnd's PDF structure cache at a per-test tmp dir.

    Without this, tests share the user's real cache at
    ``~/Library/Caches/fnd/pdf-structure/``. State from one test run
    (or from interactive usage) can leak into the next: cached entries
    with the same signature but stale content (e.g. body_md='' from a
    pre-structured-extra run) make later tests see wrong data.

    The PDF extractor caches its ExtractionCache instance in
    ``_cache_singleton`` — reset it so the patched default_cache_dir
    actually takes effect."""
    root = tmp_path / "pdf-structure-cache"
    monkeypatch.setattr("fnd.cache.default_cache_dir", lambda: root)
    from fnd.extract import pdf as _pdf

    monkeypatch.setattr(_pdf, "_cache_singleton", None)
    return root


@pytest.fixture(autouse=True)
def _quiet_preview_load_paths() -> Generator[None]:  # pyright: ignore[reportUnusedFunction]
    """Pin debounce + prefetch to 0 so cold-load assertions don't race
    the background worker. Pydantic v2 caches validators at class
    definition, so flipping ``model_fields[..].default`` needs
    ``model_rebuild(force=True)`` to take effect."""
    from fnd.config import Defaults

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
