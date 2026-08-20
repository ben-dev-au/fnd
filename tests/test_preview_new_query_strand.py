"""Regression: a new query arriving while a structural preview mount is
mid-flight must not leave the old query's container stuck in the pane.

`search_controller.run()` (new query), `clear_results()` (scope clear) and
`rerender_current()` (highlight toggle) all clear the chunk + preview caches,
drop the cached containers from the DOM, and cancel the in-flight mount — on
the assumption the cancel takes effect synchronously. It doesn't: the mount
task's `finally` runs a tick LATER and unconditionally `preview_cache.put()`s
its container back AND leaves it mounted in the pane. So the just-cleared
state is re-polluted with the previous query's half-built container — the
"stuck mid-mount after a new query" symptom.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path

import pytest

from fnd.index import build_index
from fnd.tui import FNDApp
from fnd.tui.widgets.preview_container import PreviewContainer
from tests._pilot_wait import run_search, safe_pause


@pytest.fixture
def built_index(fixtures_dir: Path, tmp_index_dir: Path) -> Path:
    build_index(roots=[fixtures_dir], index_dir=tmp_index_dir, collection="default")
    return tmp_index_dir


@pytest.mark.asyncio
async def test_new_query_during_inflight_mount_purges_stale_container(
    built_index: Path,
) -> None:
    app = FNDApp(index_dir=built_index, initial_query="results")
    async with app.run_test() as pilot:
        await safe_pause(pilot)
        assert app._search.groups, "setup — query produced no results"
        preview = app._preview

        # Gate the cold structural mount in its early-await window so it sits
        # parked while a new query runs on top of it.
        gate = asyncio.Event()

        async def _blocking_cancel_task_on(_c: object) -> None:
            await gate.wait()

        app._prefetch.cancel_task_on = _blocking_cancel_task_on  # type: ignore[assignment]

        g = app._search.groups[0]
        seq = g.hits[0].chunk_seq if g.hits else 0
        chunks = app._search.searcher.get_file_chunks(g.parent_id)  # type: ignore[union-attr]
        stale = PreviewContainer(
            parent_doc_id=g.parent_id,
            query_signature=app._search.query_signature(),
            total_chunks=len(chunks),
        )
        preview.show_progress_bar(total=len(chunks), phase="mounting…")
        preview.inflight_target = (g.parent_id, seq)
        task = asyncio.create_task(
            preview._mount_chunks_async(
                g.parent_id, seq, chunks, stale, reset_generation=preview.reset_generation
            )
        )
        preview.mount_task = task
        await safe_pause(pilot)  # park on the gate — container now mounted, pre-finalise
        assert stale.parent is not None, "setup — parked container should be mounted in DOM"
        assert getattr(stale, "_finalise_task", None) is None, "setup — must be pre-finalise"

        # New query arrives while the mount is parked mid-flight; it clears the
        # caches + DOM and cancels the parked mount.
        await run_search(pilot, app, "results")
        await safe_pause(pilot)
        await safe_pause(pilot)

        # Release the parked (now-cancelled) mount and await THIS task directly:
        # run() nulls preview.mount_task during cancellation, so
        # user_mount_in_flight() would report idle before this task's finally has
        # actually drained. Awaiting the captured task is the deterministic wait.
        gate.set()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=10.0)
        await safe_pause(pilot)

        assert stale not in set(preview.preview_cache._cache.values()), (
            "BUG: cancelled mount re-inserted the stale container into the cleared cache"
        )
        assert stale not in set(app.query(PreviewContainer)), (
            "BUG: cancelled mount left the stale container mounted in the pane (stuck mid-mount)"
        )
        assert preview.active is not stale, "BUG: stale container is still the active preview"


@pytest.mark.asyncio
async def test_superseded_mount_cancels_detached_finaliser(built_index: Path) -> None:
    """A superseded mount must cancel its detached finaliser so it can't later
    clobber the SUCCESSOR query's progress bar + inflight latch.

    The cold path spawns ``container._finalise_task`` (``_finalise_via_lock``),
    which on completion unconditionally hides the bar and clears
    ``inflight_target``. Cancelling the mount task does not cancel that detached
    task — so without the generation-guarded cancel, a stale finaliser fires
    after a new query and tears down the new query's loading state.
    """
    app = FNDApp(index_dir=built_index, initial_query="results")
    async with app.run_test() as pilot:
        await safe_pause(pilot)
        assert app._search.groups, "setup — query produced no results"
        preview = app._preview

        gate = asyncio.Event()

        async def _blocking_cancel_task_on(_c: object) -> None:
            await gate.wait()

        app._prefetch.cancel_task_on = _blocking_cancel_task_on  # type: ignore[assignment]

        g = app._search.groups[0]
        seq = g.hits[0].chunk_seq if g.hits else 0
        chunks = app._search.searcher.get_file_chunks(g.parent_id)  # type: ignore[union-attr]
        container = PreviewContainer(
            parent_doc_id=g.parent_id,
            query_signature=app._search.query_signature(),
            total_chunks=len(chunks),
        )

        # Stand-in for the detached _finalise_via_lock: parked on a gate; if it
        # ever completes it hides the bar + clears the latch — the clobber the
        # fix must prevent. Attached to the container exactly as the cold path
        # attaches the real one.
        finalise_gate = asyncio.Event()
        clobbered = {"ran": False}

        async def _fake_finaliser() -> None:
            await finalise_gate.wait()
            clobbered["ran"] = True
            preview.hide_progress_bar()
            preview.inflight_target = None

        fin_task = asyncio.create_task(_fake_finaliser())
        container._finalise_task = fin_task  # type: ignore[attr-defined]

        preview.show_progress_bar(total=len(chunks), phase="mounting…")
        preview.inflight_target = (g.parent_id, seq)
        task = asyncio.create_task(
            preview._mount_chunks_async(
                g.parent_id, seq, chunks, container, reset_generation=preview.reset_generation
            )
        )
        preview.mount_task = task
        await safe_pause(pilot)  # park on the gate

        # New query supersedes (mirrors run(): bump generation, then cancel).
        preview.bump_reset_generation()
        preview.cancel_mount_task()
        gate.set()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=10.0)
        await safe_pause(pilot)

        assert fin_task.cancelled() or fin_task.done(), (
            "superseded mount did not cancel its detached finaliser"
        )

        # Successor now owns the loading state.
        successor = ("successor-parent-id", 3)
        preview.inflight_target = successor
        preview.show_progress_bar(total=2, phase="mounting…")

        # Release the finaliser's gate; the fix already cancelled it, so it must
        # not run and clobber the successor's bar / latch.
        finalise_gate.set()
        await safe_pause(pilot)
        await safe_pause(pilot)

        assert not clobbered["ran"], (
            "BUG: superseded finaliser ran and tore down the successor's loading state"
        )
        # The progress line is no longer part of this contract: it belongs to
        # the navigation, not to the mount, so a stale finaliser has nothing to
        # hide. Covered by test_progress_navigation_session.py.
        assert preview.inflight_target == successor, (
            "BUG: stale finaliser cleared the successor's inflight latch"
        )
