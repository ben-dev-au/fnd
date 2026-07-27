"""Regression: the flat preview path must tolerate a vanished ``#preview_pane``.

Reproduced from an intermittent Windows CI failure in
``test_rapid_kind_toggles_coalesce_to_one_search``::

    textual.worker.WorkerFailed: Worker raised exception:
    NoMatches("No nodes match '#preview_pane' on Screen(id='_default')")

The chain is ``run_worker(..., group="preview-load")`` → ``dispatch_mount``
→ ``dispatch_flat_mount`` → ``FlatBufferView.ensure_shared_buffer``, which
queried ``#preview_pane`` unguarded. A search dispatched just before the
screen goes away — app quitting, screen swapped, or a test exiting its
``run_test`` block — leaves the worker querying a pane that no longer
exists, and the raise surfaces as a worker crash rather than a no-op.

Every sibling call site (``presenter`` line-wrap measurement, ``prefetch``,
``match_navigator._pane``) already treats a missing pane as "nothing to do";
this one was simply missed. The fix follows ``_pane``'s convention of
returning ``None`` rather than raising.

Timing is not exercised here — the crash is pinned deterministically by
removing the pane and driving the same code path directly.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from textual.containers import VerticalScroll

from fnd.extract.base import Block
from fnd.index import build_index
from fnd.query import FileChunk
from fnd.tui import FNDApp


@pytest.fixture
def flat_index(tmp_path: Path, tmp_index_dir: Path) -> Path:
    """A plain-text corpus: .txt routes to the flat buffer path, which is
    where the unguarded query lived."""
    root = tmp_path / "corpus"
    root.mkdir()
    for i in range(3):
        (root / f"note_{i:02d}.txt").write_text(
            f"Glimmer note {i}\n\nThis paragraph mentions glimmer several times.\n"
            "Glimmer again, for a second matching line.\n"
        )
    build_index(roots=[root], index_dir=tmp_index_dir, collection="default")
    return tmp_index_dir


def _drop_pane(app: FNDApp) -> None:
    """Remove ``#preview_pane`` outright — the end state of a teardown."""
    app.query_one("#preview_pane", VerticalScroll).remove()


@pytest.mark.asyncio
async def test_ensure_shared_buffer_returns_none_without_a_pane(flat_index: Path) -> None:
    """The lazy-mount helper reports "no pane" instead of raising."""
    app = FNDApp(index_dir=flat_index, initial_query="glimmer")
    async with app.run_test() as pilot:
        await pilot.pause()
        _drop_pane(app)
        await pilot.pause()

        assert app._flat.ensure_shared_buffer() is None


@pytest.mark.asyncio
async def test_flat_dispatch_does_not_crash_the_worker_without_a_pane(
    flat_index: Path, tmp_path: Path
) -> None:
    """The whole point: the mount the preview worker performs must be a
    no-op once the pane is gone, not a ``NoMatches`` that fails the worker."""
    app = FNDApp(index_dir=flat_index, initial_query="glimmer")
    async with app.run_test() as pilot:
        await pilot.pause()
        chunks = [
            FileChunk(
                parent_id="parent-0",
                path=str(tmp_path / "corpus" / "note_00.txt"),
                kind="txt",
                page=0,
                slide=0,
                heading_path="",
                chunk_seq=0,
                blocks=[Block(kind="p", text="Glimmer note 0")],
            )
        ]
        _drop_pane(app)
        await pilot.pause()

        # Raised NoMatches before the guard; the worker that calls this
        # reported it as WorkerFailed and took the run down with it.
        app._preview.dispatch_flat_mount("parent-0", 0, chunks)


@pytest.mark.asyncio
async def test_buffer_still_mounts_when_the_pane_is_present(flat_index: Path) -> None:
    """The guard must not cost the normal path: with a pane present the
    shared buffer is still created and parented."""
    app = FNDApp(index_dir=flat_index, initial_query="glimmer")
    async with app.run_test() as pilot:
        await pilot.pause()

        buf = app._flat.ensure_shared_buffer()
        assert buf is not None
        assert buf.parent is not None
        # Idempotent: a second call reuses the mounted widget.
        assert app._flat.ensure_shared_buffer() is buf
