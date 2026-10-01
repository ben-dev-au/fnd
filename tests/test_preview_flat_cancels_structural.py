"""Regression: navigating to a flat (PDF/TXT) result must stop the structural
mount it is replacing.

``dispatch_mount`` routes flat formats to ``dispatch_flat_mount`` and returns
*before* the ``cancel_mount_task()`` the structural branch does. So a structural
mount already in flight for the previous file kept running; when it finished it
called ``activate_container``, which hides every ``LineBufferPreview`` and shows
its own container — the preview ends up displaying the file the user navigated
AWAY from, while the cursor sits on the flat one.

``schedule_load``'s own navigate-away cancel does not cover it: that cancel is
gated on ``self.active.parent_doc_id`` differing from the target, and the flat
path sets ``_preview.active = None``, so with a flat preview on screen there is
no active parent to compare and the cancel never fires.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path

import pytest

from fnd.index import build_index
from fnd.query import FileChunk, FileGroup
from fnd.tui import FNDApp
from fnd.tui.preview_dispatcher import choose_preview_mode
from fnd.tui.widgets.preview_container import PreviewContainer
from tests._pilot_wait import safe_pause, wait_until


@pytest.fixture
def mixed_index(tmp_path: Path, tmp_index_dir: Path) -> Path:
    """Markdown (structural path) alongside plain text (flat path)."""
    root = tmp_path / "corpus"
    root.mkdir()
    for i in range(3):
        (root / f"note_{i:02d}.md").write_text(
            f"# Apples {i}\n\nThis note is about apples for query matching.\n\n"
            f"## More {i}\n\nAnother apple paragraph about apples here.\n"
        )
    for i in range(2):
        (root / f"plain_{i}.txt").write_text(
            "\n".join(f"line {n} mentioning apples with padding" for n in range(120))
        )
    build_index(roots=[root], index_dir=tmp_index_dir, collection="default")
    return tmp_index_dir


def _pick(app: FNDApp, *, flat: bool) -> tuple[FileGroup, list[FileChunk]]:
    assert app._search.searcher is not None
    for g in app._search.groups:
        chunks = app._search.searcher.get_file_chunks(g.parent_id)
        if not chunks:
            continue
        if (choose_preview_mode(chunks) == "flat") is flat:
            return g, chunks
    pytest.skip(f"fixture corpus has no {'flat' if flat else 'structural'} result")


@pytest.mark.asyncio
async def test_flat_navigation_cancels_an_inflight_structural_mount(mixed_index: Path) -> None:
    app = FNDApp(index_dir=mixed_index, initial_query="apples")
    async with app.run_test() as pilot:
        await safe_pause(pilot)
        assert app._search.groups, "setup — query produced no results"
        preview = app._preview
        # The app's own first preview must land first: the parked mount below
        # takes its `mount_task` slot, so a load still running there is never cancelled.
        await wait_until(
            pilot,
            lambda: preview.showing_parent() is not None and not preview.pipeline_busy(),
            message="the first result's preview never landed",
        )
        struct_g, struct_chunks = _pick(app, flat=False)
        flat_g, flat_chunks = _pick(app, flat=True)

        # Park a structural mount in its early-await window so it is unambiguously
        # in flight when the flat navigation lands.
        gate = asyncio.Event()

        async def _blocking_cancel_task_on(_c: object) -> None:
            await gate.wait()

        app._prefetch.cancel_task_on = _blocking_cancel_task_on  # type: ignore[assignment]

        container = PreviewContainer(
            parent_doc_id=struct_g.parent_id,
            query_signature=app._search.query_signature(),
            total_chunks=len(struct_chunks),
        )
        task = asyncio.create_task(
            preview._mount_chunks_async(
                struct_g.parent_id,
                struct_chunks[0].chunk_seq,
                struct_chunks,
                container,
                reset_generation=preview.reset_generation,
            )
        )
        preview.mount_task = task
        await safe_pause(pilot)
        assert not task.done(), "setup — the structural mount should be parked in flight"

        # Navigate to the flat result.
        preview.dispatch_mount(flat_g.parent_id, flat_chunks[0].chunk_seq, flat_chunks)

        assert task.cancelled() or task.cancelling() > 0, (
            "a flat navigation must cancel the structural mount it replaces — "
            "otherwise that mount activates its container and displays the wrong file"
        )

        gate.set()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        for _ in range(6):
            await safe_pause(pilot)

        buf = app._flat.active_buffer
        assert buf is not None, "the flat buffer must be the visible preview"
        assert not buf.has_class("-hidden"), "the flat buffer must not be hidden again"
        assert preview.active is None, (
            "the superseded structural container must not have taken the pane back"
        )


@pytest.mark.asyncio
async def test_a_decode_that_finishes_after_a_newer_navigation_does_not_show_its_file(
    mixed_index: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading

    app = FNDApp(index_dir=mixed_index, initial_query="apples")
    async with app.run_test() as pilot:
        preview = app._preview
        await wait_until(
            pilot,
            lambda: preview.showing_parent() is not None and not preview.pipeline_busy(),
            message="the first result's preview never landed",
        )
        searcher = app._search.searcher
        assert searcher is not None
        shown = preview.showing_parent()
        cold = next(
            g
            for g in app._search.groups
            if g.parent_id != shown
            and choose_preview_mode(searcher.get_file_chunks(g.parent_id)) != "flat"
        )
        flat_g, flat_chunks = _pick(app, flat=True)
        preview.chunk_cache.pop(cold.parent_id, None)
        preview.chunk_cache[flat_g.parent_id] = flat_chunks

        release = threading.Event()
        decode = searcher.get_file_chunks

        def held(parent_id: str, **kwargs: object) -> list[FileChunk]:
            if parent_id == cold.parent_id:
                release.wait(10)
            return decode(parent_id, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(searcher, "get_file_chunks", held)
        preview.render_full_doc(cold.parent_id, focus_chunk_seq=0)
        assert preview.decode_worker is not None, "setup: the cold file should be decoding"
        preview.render_full_doc(flat_g.parent_id, focus_chunk_seq=flat_chunks[0].chunk_seq)
        assert app._flat.active_buffer is not None, "setup: the cached flat file shows at once"

        release.set()
        await wait_until(
            pilot,
            lambda: cold.parent_id in preview.chunk_cache,
            message="the held decode never reported back",
        )
        await wait_until(pilot, lambda: not preview.pipeline_busy(), message="never settled")
        assert app._flat.active_buffer is not None, "the late decode replaced the newer preview"
        assert preview.active is None
