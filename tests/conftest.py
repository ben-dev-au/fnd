"""Shared pytest fixtures."""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
from collections.abc import Generator
from pathlib import Path

import pytest
from textual.pilot import Pilot, WaitForScreenTimeout

# ── Pilot patches: tolerate internal _wait_for_screen timeouts ─────
#
# Under full-suite CPU load, ``Pilot.pause()`` and ``Pilot.press()``
# call ``_wait_for_screen(timeout=30.0)`` which can raise
# ``WaitForScreenTimeout`` even when the test would otherwise pass.
# The widget message queue eventually drains; the 30 s wall-clock
# bound just lapses first.
#
# We wrap both methods so the timeout becomes a soft yield to the
# event loop. State assertions in tests must then use predicate
# polling (see ``tests/_pilot_wait.wait_until``) instead of relying
# on one ``pilot.pause()`` for a deterministic settle.
_orig_pause = Pilot.pause
_orig_press = Pilot.press


async def _safe_pause(self: Pilot, delay: float | None = None) -> None:  # type: ignore[type-arg]
    try:
        await _orig_pause(self, delay)
    except WaitForScreenTimeout:
        for _ in range(8):
            await asyncio.sleep(0)


async def _safe_press(self: Pilot, *keys: str) -> None:  # type: ignore[type-arg]
    try:
        await _orig_press(self, *keys)
    except WaitForScreenTimeout:
        for _ in range(8):
            await asyncio.sleep(0)


Pilot.pause = _safe_pause  # type: ignore[method-assign]
Pilot.press = _safe_press  # type: ignore[method-assign]


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
    is installed.

    Uses ``importlib.import_module`` instead of a static ``import``
    so static type-checkers (pyright) don't blow up when the package
    isn't present in the analysis environment."""
    spec = importlib.util.find_spec("pymupdf4llm")
    if spec is None:
        return False
    try:
        mod = importlib.import_module("pymupdf4llm")
    except Exception:
        return False
    return hasattr(mod, "to_markdown")


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
def isolated_seen_log(  # pyright: ignore[reportUnusedFunction]
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Redirect the non-PDF "have we seen this content?" marker store
    at a per-test temp path. Without isolation, a marker written by
    one test (e.g. for the ubiquitous ``# A\\n`` markdown fixture)
    would make a later test's first-run assertion of ``indexed_newly``
    fail with ``indexed_already`` instead."""
    seen_root = tmp_path / "seen-log"
    monkeypatch.setattr("fnd.seen_log._seen_root", lambda: seen_root)
    return seen_root


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
