"""A mount task a same-file jump replaces stays reachable by cancel_mount_task,
which a new query and quit rely on to stop preview work (#201)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from fnd.index import build_index
from fnd.tui import FNDApp
from fnd.tui.preview.liveness import is_live
from fnd.tui.preview_dispatcher import choose_preview_mode
from tests._pilot_wait import wait_until


@pytest.fixture
def built_index(tmp_path: Path, tmp_index_dir: Path) -> Path:
    root = tmp_path / "corpus"
    root.mkdir()
    sections = "\n\n".join(f"## Part {i}\n\nThe apple of part {i}." for i in range(6))
    (root / "orchard.md").write_text(f"# Orchard\n\n{sections}\n")
    build_index(roots=[root], index_dir=tmp_index_dir, collection="default")
    return tmp_index_dir


@pytest.mark.asyncio
async def test_a_replaced_mount_task_is_still_cancelled(built_index: Path) -> None:
    """The jump leaves the earlier task running; cancel_mount_task still stops it."""
    app = FNDApp(index_dir=built_index, initial_query="apple")
    async with app.run_test() as pilot:
        preview = app._preview
        await wait_until(pilot, lambda: bool(app._search.groups), message="no results")
        g = app._search.groups[0]
        assert app._search.searcher is not None
        chunks = app._search.searcher.get_file_chunks(g.parent_id)
        assert len(chunks) > 1
        assert choose_preview_mode(chunks) != "flat"
        preview.dispatch_mount(g.parent_id, chunks[0].chunk_seq, chunks)
        await wait_until(
            pilot,
            lambda: (
                preview.active is not None
                and is_live(preview.active)
                and preview.active.parent_doc_id == g.parent_id
                and preview.active.is_complete
            ),
            message="setup: the file never mounted completely",
        )

        gate = asyncio.Event()

        async def _parked(*_args: object) -> None:
            await gate.wait()

        preview._settled_instant_scroll = _parked  # type: ignore[assignment,method-assign]
        preview.dispatch_mount(g.parent_id, chunks[0].chunk_seq, chunks)
        first = preview.mount_task
        assert first is not None
        await asyncio.sleep(0)
        preview.dispatch_mount(g.parent_id, chunks[-1].chunk_seq, chunks)
        assert preview.mount_task is not first
        assert not first.done()  # type: ignore[attr-defined]
        preview.cancel_mount_task()
        assert first.cancelling() > 0 or first.cancelled()  # type: ignore[attr-defined]
