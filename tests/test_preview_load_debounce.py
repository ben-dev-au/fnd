"""Leading-edge preview-load debounce.

A deliberate (settled) cursor move loads the preview immediately; a rapid
arrow-sweep that follows loads only the row it finally lands on. The leading
fire removes ~150ms of perceived nav lag from every single jump (real-terminal
measurement) while the trailing coalesce still spares mid-sweep rows."""

from __future__ import annotations

from pathlib import Path

import pytest

from fnd.config import Config, Defaults
from fnd.index import build_index
from fnd.tui import FNDApp
from tests._pilot_wait import safe_pause, wait_until


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
async def test_settled_move_loads_immediately(built_index: Path, cfg_with_debounce: Config) -> None:
    """Leading edge: a deliberate (settled) cursor move loads NOW, not after the
    debounce window. This is the fix for ~150ms of perceived nav lag on every
    single jump (real-terminal measurement)."""
    app = FNDApp(index_dir=built_index, config=cfg_with_debounce, initial_query="results")
    async with app.run_test() as pilot:
        await safe_pause(pilot)
        render_calls: list[tuple[str, int]] = []
        original = app._preview.render_full_doc

        def counted(parent_id: str, *, focus_chunk_seq: int) -> None:
            render_calls.append((parent_id, focus_chunk_seq))
            original(parent_id, focus_chunk_seq=focus_chunk_seq)

        app._preview.render_full_doc = counted  # type: ignore[method-assign]
        app._preview.cancel_pending_load()  # settled — no pending window
        app._preview.schedule_load("p1", 0)
        # Fires synchronously, before any time passes (no wait_until / sleep).
        assert render_calls == [("p1", 0)], "a settled move must load immediately"


@pytest.mark.asyncio
async def test_rapid_cursor_sweep_loads_leading_plus_final(
    built_index: Path, cfg_with_debounce: Config
) -> None:
    """Leading edge + trailing coalesce: the first (settled) highlight loads
    immediately; a rapid sweep that follows loads only its FINAL row, not every
    row it passes."""
    app = FNDApp(index_dir=built_index, config=cfg_with_debounce, initial_query="results")
    async with app.run_test() as pilot:
        await safe_pause(pilot)

        render_calls: list[tuple[str, int]] = []
        original = app._preview.render_full_doc

        def counted(parent_id: str, *, focus_chunk_seq: int) -> None:
            render_calls.append((parent_id, focus_chunk_seq))
            original(parent_id, focus_chunk_seq=focus_chunk_seq)

        app._preview.render_full_doc = counted  # type: ignore[method-assign]
        app._preview.cancel_pending_load()  # settled

        # Five highlights back-to-back: leading fires p1 now; p2–p5 coalesce.
        app._preview.schedule_load("p1", 0)
        app._preview.schedule_load("p2", 0)
        app._preview.schedule_load("p3", 0)
        app._preview.schedule_load("p4", 0)
        app._preview.schedule_load("p5", 0)
        assert render_calls == [("p1", 0)], "leading edge loads the first row now"

        # The sweep's final row lands once the window matures.
        await wait_until(
            pilot,
            lambda: len(render_calls) >= 2,
            timeout=15.0,
            message="trailing debounce never fired the final row",
        )
        assert render_calls == [("p1", 0), ("p5", 0)], render_calls


@pytest.mark.asyncio
async def test_zero_delay_dispatches_synchronously(
    built_index: Path,
) -> None:
    """When ``preview_load_debounce_ms`` is 0 the load fires inline —
    the legacy / test-time behaviour."""
    cfg = Config(defaults=Defaults(preview_load_debounce_ms=0))
    app = FNDApp(index_dir=built_index, config=cfg, initial_query="results")
    async with app.run_test() as pilot:
        await safe_pause(pilot)
        render_calls: list[str] = []
        original = app._preview.render_full_doc

        def counted(parent_id: str, *, focus_chunk_seq: int) -> None:
            render_calls.append(parent_id)
            original(parent_id, focus_chunk_seq=focus_chunk_seq)

        app._preview.render_full_doc = counted  # type: ignore[method-assign]
        app._preview.schedule_load("only", 0)
        assert render_calls == ["only"]


@pytest.mark.asyncio
async def test_return_to_cancelled_target_redispatches(
    built_index: Path, cfg_with_debounce: Config
) -> None:
    """Overshoot-and-return during a fast sweep must not strand the preview.

    Sweeping past a file whose mount is mid-flight cancels that mount; if the
    cursor then lands back on it, the coalescing latch must not suppress the
    remount — the cancelled mount will never land it. Regression: the latch was
    cleared only on settle/new-query, so returning to a cancelled target was
    mistaken for "already in flight" and the only dispatch that would mount it
    was dedup-skipped, hanging the preview until an unrelated nav reset it.
    """
    import types

    app = FNDApp(index_dir=built_index, config=cfg_with_debounce, initial_query="results")
    async with app.run_test() as pilot:
        await safe_pause(pilot)

        renders: list[tuple[str, int]] = []
        app._preview.render_full_doc = lambda parent_id, *, focus_chunk_seq: renders.append(  # type: ignore[method-assign]
            (parent_id, focus_chunk_seq)
        )
        app._prefetch_top_results = lambda **_k: None  # type: ignore[method-assign,assignment]

        # A mount for ("target", 0) is mid-flight: latch set, a live task on the
        # loop, and a *different* file on screen so the navigate-away cancel fires.
        class _LiveTask:
            def done(self) -> bool:
                return False

            def cancel(self) -> None:
                pass

        app._preview.inflight_target = ("target", 0)
        app._preview.mount_task = _LiveTask()
        app._preview.active = types.SimpleNamespace(parent_doc_id="onscreen")  # type: ignore[assignment]

        # Overshoot to a neighbour (cancels the in-flight "target" mount) …
        app._preview.schedule_load("neighbour", 0)
        # … then land back on the original target.
        app._preview.schedule_load("target", 0)

        # Fire the matured debounce: the remount for "target" must run, not be
        # dedup-skipped against its own cancelled mount.
        app._preview.fire_pending_load()

        assert ("target", 0) in renders, (
            "returning to a target whose mount was cancelled must re-dispatch it"
        )


@pytest.mark.asyncio
async def test_query_change_cancels_pending_load(
    built_index: Path, cfg_with_debounce: Config
) -> None:
    """A new query rebuilds the results tree; any pending (coalescing) debounce
    target from the prior result set must not fire after the rebuild."""
    app = FNDApp(index_dir=built_index, config=cfg_with_debounce, initial_query="results")
    async with app.run_test() as pilot:
        await safe_pause(pilot)
        # Don't actually load the stale parents — they have no real chunk data.
        app._preview.render_full_doc = (  # type: ignore[method-assign]
            lambda parent_id, *, focus_chunk_seq: None
        )
        app._preview.cancel_pending_load()
        # First move leading-fires; the second coalesces into a PENDING trailing
        # load pointing at a parent that won't survive the next query.
        app._preview.schedule_load("stale-1", 0)
        app._preview.schedule_load("stale-parent-id", 0)
        assert app._preview.load_target == ("stale-parent-id", 0)
        # Run a fresh query: this calls _refresh_results_tree, which
        # cancels pending loads.
        app._search.run("nonsense-query-that-matches-nothing")
        await wait_until(
            pilot,
            lambda: app._preview.load_target is None and app._preview.load_timer is None,
            timeout=15.0,
            message="pending load wasn't cancelled by query change",
        )
