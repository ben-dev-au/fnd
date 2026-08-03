"""The settle-time paint check: a navigation that ends blank gets one rebuild.

Prevention at the consumption seams is the real fix for the blank-preview strand
(see ``test_preview_condemned_container.py`` and ``fnd/tui/preview/liveness.py``).
This is the backstop behind it: armed on every navigation, it verifies the
OUTCOME — is the pane actually showing the cursor's file? — rather than any one
mechanism, so a strand introduced at some future seam costs the user one extra
rebuild instead of a pane that stays blank until they navigate away and back.

It is deliberately and strictly bounded. An earlier attempt at recovery in this
subsystem re-dispatched on every failed reveal and cascaded into a re-dispatch
storm that was worse than the bug; hence one repair per target, and no repair at
all while the pipeline is still legitimately working.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from textual.pilot import Pilot

from fnd.index import build_index
from fnd.tui import FNDApp
from fnd.tui.preview.presenter import PreviewPresenter
from tests._pilot_wait import safe_pause, wait_until


@pytest.fixture
def built_index(tmp_path: Path, tmp_index_dir: Path) -> Path:
    root = tmp_path / "corpus"
    root.mkdir()
    for i in range(5):
        (root / f"note_{i:02d}.md").write_text(
            f"# Apples {i}\n\nThis note is about apples for query matching.\n\n"
            f"## More {i}\n\nAnother apple paragraph here.\n"
        )
    build_index(roots=[root], index_dir=tmp_index_dir, collection="default")
    return tmp_index_dir


async def _settled(pilot: Pilot[None], preview: PreviewPresenter) -> None:
    """Wait until the preview has painted AND the pipeline is idle.

    ``_verify_painted`` re-arms instead of repairing while a load/mount/finalize
    is still in flight — including the Phase 3 background fill that keeps running
    after first paint. A test that asserts on the repair DECISION must gate on
    that, or it is really asserting on how far the background fill happened to
    get by the time it looked (which made this file timing-dependent)."""
    await wait_until(pilot, preview.is_painted, message="preview never painted")
    await wait_until(
        pilot,
        lambda: not preview.pipeline_busy(),
        message="preview pipeline never went idle",
    )


@pytest.mark.asyncio
async def test_paint_check_rebuilds_a_stranded_blank_preview(built_index: Path) -> None:
    app = FNDApp(index_dir=built_index, initial_query="apple")
    async with app.run_test() as pilot:
        await safe_pause(pilot)
        preview = app._preview
        await _settled(pilot, preview)

        target = preview.cursor_target()
        assert target is not None, "setup — the cursor should be on a result"

        # Strand it exactly as the race did: the active container leaves the DOM
        # while ``active`` keeps pointing at it. Nothing in the normal pipeline
        # repaints this — the reveal watchdog only lifts ``-pre-reveal``.
        stranded = preview.active
        assert stranded is not None
        stranded.remove()
        for _ in range(6):
            await safe_pause(pilot)
        assert not preview.is_painted(), "setup — the pane should now be blank"

        preview._verify_painted()
        await wait_until(
            pilot, preview.is_painted, message="paint check did not rebuild the blank preview"
        )
        assert preview.active is not stranded, "the rebuild must produce a fresh container"


@pytest.mark.asyncio
async def test_paint_check_is_a_noop_when_the_preview_is_painted(built_index: Path) -> None:
    app = FNDApp(index_dir=built_index, initial_query="apple")
    async with app.run_test() as pilot:
        await safe_pause(pilot)
        preview = app._preview
        await _settled(pilot, preview)

        target = preview.cursor_target()
        assert target is not None
        before = preview.active

        rebuilt: list[str] = []
        orig = preview.render_full_doc

        def _spy(parent_id: str, *, focus_chunk_seq: int) -> None:
            rebuilt.append(parent_id)
            orig(parent_id, focus_chunk_seq=focus_chunk_seq)

        preview.render_full_doc = _spy  # type: ignore[assignment,method-assign]
        preview._verify_painted()

        assert not rebuilt, "a healthy preview must not be rebuilt by the paint check"
        assert preview.active is before


@pytest.mark.asyncio
async def test_paint_check_repairs_a_preview_showing_the_wrong_file(built_index: Path) -> None:
    """The invariant is not merely "something is painted" — it is "the pane
    shows the file the cursor is on". A stale dispatch (a late cursor echo, a
    debounce timer firing for a row the user has already left) paints a
    different file; that must be corrected too."""
    app = FNDApp(index_dir=built_index, initial_query="apple")
    async with app.run_test() as pilot:
        await safe_pause(pilot)
        preview = app._preview
        await _settled(pilot, preview)
        target = preview.cursor_target()
        assert target is not None

        # A perfectly healthy container — for the wrong file.
        other = next(
            (g for g in app._search.groups if g.parent_id != target[0]),
            None,
        )
        assert other is not None, "fixture needs at least two files"
        assert preview.active is not None
        preview.active.parent_doc_id = other.parent_id

        assert preview.is_painted(), "setup — the pane is painted, just with the wrong file"
        assert preview.showing_parent() != target[0]

        rebuilt: list[tuple[str, int]] = []
        preview.render_full_doc = lambda parent_id, *, focus_chunk_seq: rebuilt.append(  # type: ignore[assignment,method-assign]
            (parent_id, focus_chunk_seq)
        )
        preview._verify_painted()

        assert rebuilt == [target], (
            "a painted-but-wrong-file preview must be re-dispatched for the cursor's file"
        )


@pytest.mark.asyncio
async def test_paint_check_leaves_option_scan_mode_alone(built_index: Path) -> None:
    """Option/Alt+arrow scanning deliberately moves the cursor WITHOUT loading a
    preview. The check must not treat that designed divergence as a fault and
    mount the very row scan mode is avoiding."""
    app = FNDApp(index_dir=built_index, initial_query="apple")
    async with app.run_test() as pilot:
        await safe_pause(pilot)
        preview = app._preview
        await _settled(pilot, preview)

        rebuilt: list[str] = []
        preview.render_full_doc = lambda parent_id, *, focus_chunk_seq: rebuilt.append(  # type: ignore[assignment,method-assign]
            parent_id
        )
        preview.active = None  # scanning away from the loaded file
        preview._scan_move = True
        preview._verify_painted()

        assert not rebuilt, "scan mode must not be interrupted by the paint check"


@pytest.mark.asyncio
async def test_paint_check_repairs_at_most_once_per_target(built_index: Path) -> None:
    """The cascade guard: if the repair itself leaves the pane blank, the check
    must give up on that target rather than re-dispatching forever."""
    app = FNDApp(index_dir=built_index, initial_query="apple")
    async with app.run_test() as pilot:
        await safe_pause(pilot)
        preview = app._preview
        await _settled(pilot, preview)
        target = preview.cursor_target()
        assert target is not None

        rebuilt: list[str] = []
        # A rebuild that never paints — the pathological case the cap exists for.
        preview.render_full_doc = lambda parent_id, *, focus_chunk_seq: rebuilt.append(  # type: ignore[assignment,method-assign]
            parent_id
        )
        preview.active = None
        assert not preview.is_painted(), "setup — pane is blank"

        for _ in range(5):
            preview._verify_painted()

        assert len(rebuilt) == 1, f"expected exactly one repair attempt, got {len(rebuilt)}"


@pytest.mark.asyncio
async def test_paint_check_defers_while_the_pipeline_is_busy(built_index: Path) -> None:
    """A slow-but-healthy mount must never be pre-empted: while work is in
    flight the check re-arms instead of repairing."""
    import asyncio

    app = FNDApp(index_dir=built_index, initial_query="apple")
    async with app.run_test() as pilot:
        await safe_pause(pilot)
        preview = app._preview
        await _settled(pilot, preview)
        target = preview.cursor_target()
        assert target is not None

        rebuilt: list[str] = []
        preview.render_full_doc = lambda parent_id, *, focus_chunk_seq: rebuilt.append(  # type: ignore[assignment,method-assign]
            parent_id
        )
        preview.active = None
        assert not preview.is_painted(), "setup — pane is blank"

        parked = asyncio.Event()
        task = asyncio.create_task(parked.wait())
        preview.mount_task = task
        try:
            assert preview.pipeline_busy(), "setup — a live mount task means busy"
            preview._verify_painted()
            assert not rebuilt, "must not repair while a mount is still in flight"
            assert preview._paint_check is not None, "the check must re-arm instead"
        finally:
            parked.set()
            await task
