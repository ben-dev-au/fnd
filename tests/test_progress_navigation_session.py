"""One progress session per navigation, in a real app.

The frame-by-frame shape of the line is pinned in
``test_progress_navigation_shape``; what is checked here is the wiring:
that a navigation opens a session at the moment it starts, that the
session is released once the match has landed, and that the mount path's
old teardown calls can no longer take it down early.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from textual.pilot import Pilot

from fnd.index import build_index
from fnd.query import FileGroup
from fnd.tui import FNDApp
from fnd.tui.progress.bar import FNDProgressBar
from fnd.tui.progress.operations import INDEX
from tests._pilot_wait import run_search, wait_until


@pytest.fixture
def two_file_index(tmp_path: Path, tmp_index_dir: Path) -> Path:
    root = tmp_path / "docs"
    root.mkdir()
    (root / "small.md").write_text("# Small\n\ntarget one\n", encoding="utf-8")
    (root / "big.md").write_text(
        "\n".join(
            textwrap.dedent(f"""
            ## Section {i}

            target paragraph {i} with enough words to make a real chunk.
            """)
            for i in range(40)
        ),
        encoding="utf-8",
    )
    build_index(roots=[root], index_dir=tmp_index_dir, collection="default")
    return tmp_index_dir


def _make_cold(app: FNDApp, parent_id: str) -> None:
    """Undo everything the coverage sweep does that prices a file as warm."""
    app._preview.chunk_cache.pop(parent_id, None)
    app._preview.capture_store.drop_file(parent_id)
    state = app._preview.file_warm_state(parent_id)
    assert state is None or not state.is_served, (
        f"{parent_id[:8]} still reads as served, so this navigation is not cold"
    )


async def _search(pilot: Pilot[None], app: FNDApp) -> tuple[FileGroup, FileGroup]:
    await run_search(pilot, app, "target")
    small = next(g for g in app._search.groups if g.path.endswith("small.md"))
    big = next(g for g in app._search.groups if g.path.endswith("big.md"))
    return small, big


@pytest.mark.asyncio
async def test_a_navigation_opens_a_session_straight_away(two_file_index: Path) -> None:
    """The line must belong to the keypress that caused it — not appear
    once some later stage happens to get round to showing it."""
    app = FNDApp(index_dir=two_file_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        _small, big = await _search(pilot, app)
        # A committed search parks the cursor, which dispatches a preview load
        # of its own — so wait for that to land before testing a navigation.
        await wait_until(pilot, lambda: app._progress.active is None)

        # Make the navigation cold, don't hope for it. That parked load starts
        # a coverage sweep which decodes and captures NEIGHBOURS, and both are
        # inputs to the warm/cold plan — so whether this file is still cold when
        # the test gets here is the runner's timing, not the test's setup.
        _make_cold(app, big.parent_id)

        app._preview.render_full_doc(big.parent_id, focus_chunk_seq=0)
        session = app._progress.active
        assert session is not None, "navigating did not open a progress session"
        assert session.operation_id == "preview.cold"


@pytest.mark.asyncio
async def test_the_session_is_released_once_the_navigation_lands(
    two_file_index: Path,
) -> None:
    """The other half of the old bug: it also used to stick. The tracker
    releases the line when the pipeline is idle and the scroll committed."""
    app = FNDApp(index_dir=two_file_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        _small, big = await _search(pilot, app)

        app._preview.render_full_doc(big.parent_id, focus_chunk_seq=0)
        await wait_until(
            pilot,
            lambda: app._progress.active is None,
            message="progress session stranded after the navigation landed",
        )


@pytest.mark.asyncio
async def test_a_jump_inside_the_open_file_is_a_warm_navigation(
    two_file_index: Path,
) -> None:
    """Cold and warm navigations differ by more than an order of magnitude,
    so they calibrate separately — pooling them would make both estimates
    useless."""
    app = FNDApp(index_dir=two_file_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        _small, big = await _search(pilot, app)

        app._preview.render_full_doc(big.parent_id, focus_chunk_seq=0)
        await wait_until(pilot, lambda: app._progress.active is None)

        app._preview.render_full_doc(big.parent_id, focus_chunk_seq=1)
        session = app._progress.active
        assert session is not None
        assert session.operation_id == "preview.warm"


@pytest.mark.asyncio
async def test_the_mount_paths_teardown_cannot_retire_the_line(
    two_file_index: Path,
) -> None:
    """Root cause 4, at the level it actually bit. ``hide_progress_bar`` is
    called from thirteen places in the mount path, several of which run for
    a navigation that has already been superseded. It used to close whatever
    session was active; now it owns no session at all."""
    app = FNDApp(index_dir=two_file_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        _small, big = await _search(pilot, app)

        app._preview.render_full_doc(big.parent_id, focus_chunk_seq=0)
        session = app._progress.active
        assert session is not None

        app._preview.hide_progress_bar()

        assert app._progress.active is session, (
            "a mount-path teardown retired the live navigation's progress line"
        )
        assert not session.closed


@pytest.mark.asyncio
async def test_a_background_index_survives_a_navigation_in_the_real_app(
    two_file_index: Path,
) -> None:
    """The line serves two classes of work, and only one of them is a
    reaction to a keypress.

    With a single session slot this failed in the field rather than in a
    test: ``begin`` was last-writer-wins, so the first navigation retired a
    running index for good. A reindex spans hundreds of navigations, which
    made the line useless for the one operation long enough to need it.

    Driven through the real app so the second visual channel is exercised
    too — an unresolved component class raises at render time, and the
    stub bar cannot see that.
    """
    app = FNDApp(index_dir=two_file_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        _small, big = await _search(pilot, app)
        bar = app.query_one(FNDProgressBar)

        running = True
        index = app._progress.begin(
            INDEX, label="default · 3 of 40 files", sampler=lambda _s: running
        )
        # The search lands a preview, and that navigation owns the line until
        # it finishes; the index takes it over once the line is free.
        await wait_until(pilot, lambda: bar.ambient is True, timeout=5.0)
        assert bar.render() is not None, "the ambient fill style does not resolve"

        app._preview.render_full_doc(big.parent_id, focus_chunk_seq=0)
        assert bar.ambient is False, "background work painted over the user's navigation"
        nav = app._progress.active
        assert nav is not None
        assert not index.closed, "the navigation retired the background index"

        nav.close()
        await wait_until(pilot, lambda: bar.ambient is True, timeout=5.0)
        assert not index.closed
        assert bar.label == "default · 3 of 40 files"
        running = False
