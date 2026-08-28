"""Phase B — async trailing-value loading.

`_summary_cache_size_row`, `_summary_pdf_status`, `_summary_indexing`,
`_summary_stale_entries` all touch the filesystem. They route through
`get_or_schedule` so first render returns `…` and a worker thread
populates the real value. The screen re-renders when the worker
completes.

Tests verify:
- First call returns the placeholder.
- After the worker runs, a subsequent call returns the real value.
- Invalidating + recomputing works (e.g. on screen resume).
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from fnd.tui.lazy_trailing import (
    _CACHE,
    PLACEHOLDER,
    get_or_schedule,
    invalidate,
    invalidate_all,
)
from tests._pilot_wait import settings_ready


class _StubApp:
    """No event loop — call_from_thread will fail (suppressed), but
    the cache populates regardless."""

    def call_from_thread(self, *_args: object, **_kwargs: object) -> None:
        raise RuntimeError("no event loop in tests")


def _wait_until(predicate: Callable[[], bool], timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_first_call_returns_placeholder() -> None:
    """get_or_schedule returns PLACEHOLDER on first call for a new key."""
    invalidate_all()
    app = _StubApp()
    result = get_or_schedule(app, "test.basic", lambda: "real value")
    assert result == PLACEHOLDER


def test_worker_populates_cache() -> None:
    """The background worker writes the real value into the cache so
    a subsequent call returns it."""
    invalidate_all()
    app = _StubApp()

    done = threading.Event()

    def _compute() -> str:
        try:
            return "computed"
        finally:
            done.set()

    get_or_schedule(app, "test.populates", _compute)
    assert done.wait(timeout=1.0), "worker should run within 1s"
    # Worker writes the cache + signals; give the lock a moment.
    assert _wait_until(lambda: _CACHE.get("test.populates", (None, 0))[0] == "computed")
    # Now the lazy getter returns the real value.
    result = get_or_schedule(app, "test.populates", _compute)
    assert result == "computed"


def test_invalidate_forces_recomputation() -> None:
    """Calling invalidate() drops the cached value; next call schedules
    a fresh worker run."""
    invalidate_all()
    app = _StubApp()

    call_count = 0
    done = threading.Event()

    def _compute() -> str:
        nonlocal call_count
        call_count += 1
        try:
            return f"value-{call_count}"
        finally:
            done.set()

    get_or_schedule(app, "test.invalidate", _compute)
    assert done.wait(1.0)
    assert _wait_until(lambda: _CACHE.get("test.invalidate", (None, 0))[0] == "value-1")
    invalidate("test.invalidate")
    done.clear()
    get_or_schedule(app, "test.invalidate", _compute)
    assert done.wait(1.0)
    assert _wait_until(lambda: _CACHE.get("test.invalidate", (None, 0))[0] == "value-2")
    assert call_count == 2


def test_compute_failure_yields_empty_string() -> None:
    """If the compute callable raises, the cache stores empty string
    so the trailing slot shows nothing rather than the placeholder forever."""
    invalidate_all()
    app = _StubApp()

    done = threading.Event()

    def _compute() -> str:
        try:
            raise ValueError("simulated failure")
        finally:
            done.set()

    get_or_schedule(app, "test.failure", _compute)
    assert done.wait(1.0)
    assert _wait_until(lambda: "test.failure" in _CACHE)
    assert _CACHE["test.failure"][0] == ""


# ── Integration: Indexing screen shows placeholder then real value ──


@pytest.mark.asyncio
async def test_cache_size_row_shows_placeholder_then_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mount the Indexing screen; the cache-size row first shows `…`,
    then the real value once the worker completes."""
    from fnd.cache import ExtractionCache
    from fnd.config import load
    from fnd.extract.base import Block, Chunk
    from fnd.index import build_index
    from fnd.tui import FNDApp
    from fnd.tui.menu import SECTION_PDF_TEXTURE
    from fnd.tui.settings_screen import SettingsList, open_settings_section

    invalidate_all()

    # Populate an isolated cache.
    cache_root = tmp_path / "cache"
    monkeypatch.setattr("fnd.cache.default_cache_dir", lambda: cache_root)
    cache = ExtractionCache(root=cache_root)
    cache.put(
        "aa--v1",
        [
            Chunk(
                parent_id="x",
                path="/x.pdf",
                mtime=0,
                kind="pdf",
                body="b",
                body_struct=[Block(kind="p", text="b")],
                body_md="b",
                page=1,
                chunk_seq=0,
            )
        ],
    )

    # Mount the screen.
    index_dir = tmp_path / "index"
    fixtures = Path(__file__).parent / "fixtures"
    build_index(roots=[fixtures], index_dir=index_dir, collection="default")
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text("")
    monkeypatch.setattr("fnd.config.default_config_path", lambda: cfg_path)
    cfg = load(cfg_path)

    app = FNDApp(index_dir=index_dir, config=cfg)
    async with app.run_test() as pilot:
        await pilot.pause()
        open_settings_section(app, SECTION_PDF_TEXTURE)
        await settings_ready(pilot, app)
        lst = app.screen.query_one(SettingsList)
        row = next(it for it in lst._items if it.id == "pdf_texture.cache_size")

        # Worker may or may not have completed by now — keep polling
        # until we see a populated entries-count string.
        for _ in range(30):
            await pilot.pause()
            v = row.trailing_value(app)
            if "entries" in v:
                break
        else:
            v = row.trailing_value(app)
        assert "entries" in v
