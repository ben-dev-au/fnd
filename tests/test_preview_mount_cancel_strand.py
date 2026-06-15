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
        task = asyncio.create_task(preview._mount_chunks_async(g.parent_id, seq, chunks, container))
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
