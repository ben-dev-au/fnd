"""Regression: a cold preview mount cancelled in its early-await phase —
before the detached finalize task (the only thing that hides the progress
bar + releases the in-flight latch) is spawned — must not strand the bar.

The symptom was "the loading bar gets stuck until I navigate to a different
file and back": cancel_mount_task cancels the mount but hides nothing, the
finally only hid on is_complete, and no finalize task existed yet — so the
bar stayed up forever and the inflight latch kept a same-file re-load from
re-dispatching.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from fnd.index import build_index
from fnd.tui import FNDApp
from tests._pilot_wait import safe_pause


@pytest.fixture
def built_index(fixtures_dir: Path, tmp_index_dir: Path) -> Path:
    build_index(roots=[fixtures_dir], index_dir=tmp_index_dir, collection="default")
    return tmp_index_dir


@pytest.mark.asyncio
async def test_cancel_during_early_mount_does_not_strand_progress_bar(
    built_index: Path,
) -> None:
    app = FNDApp(index_dir=built_index, initial_query="blue penguin sandwich")
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app._search.groups, "setup — query produced no results"

        preview = app._preview
        from fnd.tui.widgets.preview_container import PreviewContainer

        # Park a cold structural mount in its EARLY-await window: cancel_task_on
        # is awaited (presenter ~line 1304) BEFORE the finalize task is spawned
        # (~1352). Block it on an event we never set so the mount task sits
        # exactly in the vulnerable window.
        gate = asyncio.Event()

        async def _blocking_cancel_task_on(_container: object) -> None:
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

        # Mirror the real pre-mount state: bar shown, latch set, mount task
        # running — then drive the structural cold mount directly so routing
        # (flat vs structural, warm-cache) can't change the path under test.
        preview.show_progress_bar(total=len(chunks), phase="mounting…")
        preview.inflight_target = (g.parent_id, seq)
        task = asyncio.create_task(
            preview._mount_chunks_async(
                g.parent_id, seq, chunks, container, reset_generation=preview.reset_generation
            )
        )
        preview.mount_task = task

        # Let the task reach and park on the blocked early await.
        await safe_pause(pilot)
        assert app._progress.active is not None, (
            "setup — the cold mount should have the progress bar open"
        )
        assert getattr(container, "_finalize_task", None) is None, (
            "setup — mount must be parked BEFORE the finalize task is spawned"
        )

        # Cancel the mount while parked pre-finalize — the strand condition.
        preview.cancel_mount_task()
        await safe_pause(pilot)
        await safe_pause(pilot)

        assert app._progress.active is None, (
            "BUG: progress bar stranded after a cold mount was cancelled before "
            "its finalize task spawned (the 'stuck loading until I switch files' bug)"
        )
        assert preview.inflight_target is None, (
            "inflight latch not released on cancel — a same-file re-load would "
            "dedup out and never re-mount"
        )

        gate.set()  # release the blocked coroutine so the loop can drain


@pytest.mark.asyncio
async def test_exception_during_early_mount_does_not_strand_progress_bar(
    built_index: Path,
) -> None:
    """A mount that FAILS (not cancelled) in its early-await phase must also
    clean up. cancel_mount_task nulls mount_task; an exception does not — so
    the finally checks `mount_task is current_task()` too, else the bar would
    strand on any non-cancellation failure (e.g. a MountError)."""
    import contextlib

    app = FNDApp(index_dir=built_index, initial_query="blue penguin sandwich")
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app._search.groups, "setup — query produced no results"

        preview = app._preview
        from fnd.tui.widgets.preview_container import PreviewContainer

        async def _raising_cancel_task_on(_container: object) -> None:
            raise RuntimeError("simulated early-mount failure")

        app._prefetch.cancel_task_on = _raising_cancel_task_on  # type: ignore[assignment]

        g = app._search.groups[0]
        seq = g.hits[0].chunk_seq if g.hits else 0
        chunks = app._search.searcher.get_file_chunks(g.parent_id)  # type: ignore[union-attr]
        container = PreviewContainer(
            parent_doc_id=g.parent_id,
            query_signature=app._search.query_signature(),
            total_chunks=len(chunks),
        )

        preview.show_progress_bar(total=len(chunks), phase="mounting…")
        preview.inflight_target = (g.parent_id, seq)
        task = asyncio.create_task(
            preview._mount_chunks_async(
                g.parent_id, seq, chunks, container, reset_generation=preview.reset_generation
            )
        )
        preview.mount_task = task  # NOT nulled — the mount fails, it isn't cancelled

        # Drain the task (it raises); the finally must still run.
        with contextlib.suppress(RuntimeError):
            await task
        await safe_pause(pilot)

        assert app._progress.active is None, (
            "BUG: progress bar stranded after a cold mount FAILED before its "
            "finalize task spawned (mount_task is current_task path)"
        )
        assert preview.inflight_target is None, "inflight latch not released on mount failure"


@pytest.mark.asyncio
async def test_early_cancel_does_not_clobber_successor_decode_bar(
    built_index: Path,
) -> None:
    """A mount cancelled in its early-await window must NOT hide a SUCCESSOR's
    progress bar or clear its latch. The uncached decode path cancels the old
    mount (nulling mount_task) and opens a new "decoding…" session without
    reassigning mount_task — so mount_task being None is NOT enough to prove
    ownership; the cleanup also checks the inflight latch still points at this
    target."""
    app = FNDApp(index_dir=built_index, initial_query="blue penguin sandwich")
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app._search.groups, "setup — query produced no results"

        preview = app._preview
        from fnd.tui.widgets.preview_container import PreviewContainer

        gate = asyncio.Event()

        async def _blocking_cancel_task_on(_container: object) -> None:
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
        preview.show_progress_bar(total=len(chunks), phase="mounting…")
        preview.inflight_target = (g.parent_id, seq)
        task = asyncio.create_task(
            preview._mount_chunks_async(
                g.parent_id, seq, chunks, container, reset_generation=preview.reset_generation
            )
        )
        preview.mount_task = task
        await safe_pause(pilot)
        assert getattr(container, "_finalize_task", None) is None, "setup — must be pre-finalize"

        # A successor (uncached) decode now owns the loading state: a DIFFERENT
        # inflight target, and mount_task nulled — exactly what the decode path
        # leaves behind (cancel_mount_task + show_progress_bar, no mount_task).
        successor_target = ("successor-parent-id", 7)
        preview.inflight_target = successor_target

        preview.cancel_mount_task()  # cancels M1, nulls mount_task
        await safe_pause(pilot)
        await safe_pause(pilot)

        assert app._progress.active is not None, (
            "BUG: a cancelled early mount hid the SUCCESSOR's progress bar"
        )
        assert preview.inflight_target == successor_target, (
            "BUG: a cancelled early mount cleared the SUCCESSOR's inflight latch"
        )

        gate.set()
