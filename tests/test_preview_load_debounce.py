"""Rapid cursor sweeps through the results tree should kick off at
most one preview load — on the row the cursor finally lands on."""

from __future__ import annotations

from pathlib import Path

import pytest

from fnd.config import Config, Defaults
from fnd.index import build_index
from fnd.tui import FNDApp


@pytest.fixture
def built_index(fixtures_dir: Path, tmp_index_dir: Path) -> Path:
    build_index(roots=[fixtures_dir], index_dir=tmp_index_dir, collection="default")
    return tmp_index_dir


@pytest.fixture
def cfg_with_debounce() -> Config:
    """Config with an explicit, non-zero debounce so the timing path is
    actually exercised. The autouse ``_no_preview_load_debounce``
    fixture zeroes the *class default*, but a Config passed by hand
    takes precedence."""
    return Config(defaults=Defaults(preview_load_debounce_ms=150))


@pytest.mark.asyncio
async def test_rapid_cursor_sweep_dispatches_once(
    built_index: Path, cfg_with_debounce: Config
) -> None:
    """Several quick highlights only fire one ``_render_full_doc``."""
    app = FNDApp(index_dir=built_index, config=cfg_with_debounce, initial_query="results")
    async with app.run_test() as pilot:
        await pilot.pause()

        render_calls: list[tuple[str, int]] = []
        original = app._render_full_doc

        def counted(parent_id: str, *, focus_chunk_seq: int) -> None:
            render_calls.append((parent_id, focus_chunk_seq))
            original(parent_id, focus_chunk_seq=focus_chunk_seq)

        app._render_full_doc = counted  # type: ignore[method-assign]

        # Schedule five highlights back-to-back. Only the last one
        # should win once the timer fires.
        app._schedule_preview_load("p1", 0)
        app._schedule_preview_load("p2", 0)
        app._schedule_preview_load("p3", 0)
        app._schedule_preview_load("p4", 0)
        app._schedule_preview_load("p5", 0)
        assert render_calls == [], "no load should fire before the timer matures"

        # Wait past the 150 ms debounce.
        await pilot.pause(0.3)

        assert len(render_calls) == 1, render_calls
        assert render_calls[0][0] == "p5"


@pytest.mark.asyncio
async def test_zero_delay_dispatches_synchronously(
    built_index: Path,
) -> None:
    """When ``preview_load_debounce_ms`` is 0 the load fires inline —
    the legacy / test-time behaviour."""
    cfg = Config(defaults=Defaults(preview_load_debounce_ms=0))
    app = FNDApp(index_dir=built_index, config=cfg, initial_query="results")
    async with app.run_test() as pilot:
        await pilot.pause()
        render_calls: list[str] = []
        original = app._render_full_doc

        def counted(parent_id: str, *, focus_chunk_seq: int) -> None:
            render_calls.append(parent_id)
            original(parent_id, focus_chunk_seq=focus_chunk_seq)

        app._render_full_doc = counted  # type: ignore[method-assign]
        app._schedule_preview_load("only", 0)
        assert render_calls == ["only"]


@pytest.mark.asyncio
async def test_query_change_cancels_pending_load(
    built_index: Path, cfg_with_debounce: Config
) -> None:
    """A new query rebuilds the results tree; any in-flight debounce
    target from the prior result set must not fire after rebuild."""
    app = FNDApp(index_dir=built_index, config=cfg_with_debounce, initial_query="results")
    async with app.run_test() as pilot:
        await pilot.pause()
        # Arm a debounced load that points at a parent_id we won't have
        # any more after the rebuild.
        app._schedule_preview_load("stale-parent-id", 0)
        assert app._preview_load_target == ("stale-parent-id", 0)
        # Run a fresh query: this calls _refresh_results_tree, which
        # cancels pending loads.
        app._run_query("nonsense-query-that-matches-nothing")
        await pilot.pause()
        assert app._preview_load_target is None
        assert app._preview_load_timer is None
