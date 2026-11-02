"""A mount task a same-file jump replaces stays reachable by cancel_mount_task,
which a new query and quit rely on to stop preview work, and its late freeze
sweep never freezes the newer jump's focus (#201)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from fnd.index import build_index
from fnd.tui import FNDApp
from fnd.tui.preview import tuning
from fnd.tui.preview.liveness import is_live
from fnd.tui.preview_dispatcher import choose_preview_mode
from fnd.tui.preview_scroll import ScrollAnchor
from fnd.tui.widgets.markdown import FNDMarkdown
from fnd.tui.widgets.preview_container import PreviewContainer
from tests._pilot_wait import wait_until


@pytest.fixture
def built_index(tmp_path: Path, tmp_index_dir: Path) -> Path:
    root = tmp_path / "corpus"
    root.mkdir()
    sections = "\n\n".join(f"## Part {i}\n\nThe apple of part {i}." for i in range(6))
    (root / "orchard.md").write_text(f"# Orchard\n\n{sections}\n")
    build_index(roots=[root], index_dir=tmp_index_dir, collection="default")
    return tmp_index_dir


def _laid_out(container: PreviewContainer, seq: int) -> bool:
    widget = container.chunk_widgets[seq]
    return isinstance(widget, FNDMarkdown) and widget.size.height > 0


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


@pytest.mark.asyncio
@pytest.mark.parametrize("replaced_settles", ["first", "last"])
async def test_a_replaced_jumps_sweep_leaves_the_new_focus_live(
    built_index: Path, monkeypatch: pytest.MonkeyPatch, replaced_settles: str
) -> None:
    """A replaced jump's freeze sweep, in either order, never freezes the newer focus."""
    monkeypatch.setattr(tuning, "PAINT_CHECK_MS", 60_000)
    monkeypatch.setenv("_FND_NO_FREEZE", "1")
    app = FNDApp(index_dir=built_index, initial_query="apple")
    async with app.run_test() as pilot:
        preview = app._preview
        await wait_until(pilot, lambda: bool(app._search.groups), message="no results")
        g = app._search.groups[0]
        assert app._search.searcher is not None
        chunks = app._search.searcher.get_file_chunks(g.parent_id)
        first_seq, last_seq = chunks[0].chunk_seq, chunks[-1].chunk_seq
        preview.render_full_doc(g.parent_id, focus_chunk_seq=last_seq)
        await wait_until(
            pilot,
            lambda: (
                preview.active is not None
                and is_live(preview.active)
                and preview.active.is_complete
                and not preview.pipeline_busy()
            ),
            message="setup: the file never mounted completely",
        )
        container = preview.active
        assert container is not None
        await wait_until(
            pilot,
            lambda: _laid_out(container, first_seq) and _laid_out(container, last_seq),
            message="setup: both chunks must be live and laid out",
        )
        monkeypatch.delenv("_FND_NO_FREEZE")
        await preview._freeze_chunks_outside_window(container, chunks, len(chunks) - 1, len(chunks))
        assert not isinstance(container.chunk_widgets[first_seq], FNDMarkdown)
        assert isinstance(container.chunk_widgets[last_seq], FNDMarkdown)

        replaced_gate, newer_gate = asyncio.Event(), asyncio.Event()
        real_settle = preview.await_match_settled
        calls: list[None] = []

        async def _held(*args: object, **kwargs: object) -> None:
            calls.append(None)
            if len(calls) == 1:
                await replaced_gate.wait()
            elif len(calls) == 2 and replaced_settles == "first":
                await newer_gate.wait()
            await real_settle(*args, **kwargs)  # type: ignore[arg-type]

        preview.await_match_settled = _held  # type: ignore[assignment,method-assign]
        preview.render_full_doc(g.parent_id, focus_chunk_seq=first_seq)
        replaced = preview.mount_task
        assert replaced is not None
        await wait_until(pilot, lambda: len(calls) >= 1, message="the first jump never settled")
        preview.render_full_doc(g.parent_id, focus_chunk_seq=last_seq)
        newer = preview.mount_task
        assert newer is not None
        assert newer is not replaced
        first_done, second_done = (
            (replaced, newer) if replaced_settles == "first" else (newer, replaced)
        )
        if replaced_settles == "first":
            await wait_until(pilot, lambda: len(calls) >= 2, message="the newer jump never settled")
            replaced_gate.set()
        await wait_until(pilot, first_done.done, message="the first task never finished")  # type: ignore[attr-defined]
        assert isinstance(container.chunk_widgets[last_seq], FNDMarkdown)
        newer_gate.set()
        replaced_gate.set()
        await wait_until(pilot, second_done.done, message="the second task never finished")  # type: ignore[attr-defined]
        assert isinstance(container.chunk_widgets[last_seq], FNDMarkdown)
        assert not isinstance(container.chunk_widgets[first_seq], FNDMarkdown)


@pytest.mark.asyncio
@pytest.mark.parametrize("missing_focus", [False, True])
async def test_a_sweep_whose_window_misses_the_latest_focus_freezes_that_window(
    built_index: Path, monkeypatch: pytest.MonkeyPatch, missing_focus: bool
) -> None:
    """Only the latest real focus outranks a sweep's window; a stale window freezes."""
    monkeypatch.setattr(tuning, "PAINT_CHECK_MS", 60_000)
    monkeypatch.setenv("_FND_NO_FREEZE", "1")
    app = FNDApp(index_dir=built_index, initial_query="apple")
    async with app.run_test() as pilot:
        preview = app._preview
        await wait_until(pilot, lambda: bool(app._search.groups), message="no results")
        g = app._search.groups[0]
        assert app._search.searcher is not None
        chunks = app._search.searcher.get_file_chunks(g.parent_id)
        first_seq, last_seq = chunks[0].chunk_seq, chunks[-1].chunk_seq
        preview.render_full_doc(g.parent_id, focus_chunk_seq=last_seq)
        await wait_until(
            pilot,
            lambda: (
                preview.active is not None
                and is_live(preview.active)
                and preview.active.is_complete
                and not preview.pipeline_busy()
            ),
            message="setup: the file never mounted completely",
        )
        container = preview.active
        assert container is not None
        await wait_until(
            pilot,
            lambda: _laid_out(container, first_seq) and _laid_out(container, last_seq),
            message="setup: both chunks must be live and laid out before the sweep",
        )
        monkeypatch.delenv("_FND_NO_FREEZE")
        if missing_focus:
            absent = max(c.chunk_seq for c in chunks) + 1
            app._preview_scroll.arm(ScrollAnchor(g.parent_id, absent))
        await preview._freeze_chunks_outside_window(container, chunks, 0, 1)
        assert isinstance(container.chunk_widgets[first_seq], FNDMarkdown) is missing_focus
        assert isinstance(container.chunk_widgets[last_seq], FNDMarkdown) is not missing_focus
