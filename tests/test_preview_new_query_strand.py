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
from pathlib import Path

import pytest

from fnd.index import build_index
from fnd.tui import FNDApp
from fnd.tui.widgets.preview_container import PreviewContainer
from tests._pilot_wait import safe_pause, wait_until


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
        await safe_pause(pilot)  # park on the gate — container now mounted, pre-finalize
        assert stale.parent is not None, "setup — parked container should be mounted in DOM"
        assert getattr(stale, "_finalize_task", None) is None, "setup — must be pre-finalize"

        # New query arrives while the mount is parked mid-flight; it clears the
        # caches + DOM and cancels the parked mount.
        app._search.run("results")
        await safe_pause(pilot)
        await safe_pause(pilot)

        # Release the parked (now-cancelled) mount so its finally runs.
        gate.set()
        await wait_until(
            pilot,
            lambda: not preview.user_mount_in_flight(),
            timeout=10.0,
            message="cancelled mount never drained",
        )
        await safe_pause(pilot)
        await safe_pause(pilot)

        assert app._progress.active is None, "progress bar stranded after new query"
        assert stale not in set(preview.preview_cache._cache.values()), (
            "BUG: cancelled mount re-inserted the stale container into the cleared cache"
        )
        assert stale not in set(app.query(PreviewContainer)), (
            "BUG: cancelled mount left the stale container mounted in the pane (stuck mid-mount)"
        )
        assert preview.active is not stale, "BUG: stale container is still the active preview"
