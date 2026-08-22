"""Scroll-driven lazy mount: long files mount more chunks as the user
scrolls toward the boundary of the currently-mounted region.

Before this path existed, the structural preview was locked to a fixed
focus ± _VISIBLE_FIRST_* window; scrolling past it just hit a wall. Now
``MatchAwareScroll.watch_scroll_y`` notifies the app and the app mounts
the next ``LAZY_MOUNT_BATCH`` chunks in the scroll direction.
"""

from __future__ import annotations

import asyncio
import textwrap
from pathlib import Path
from typing import Any, cast

import pytest
from textual.containers import VerticalScroll
from textual.pilot import Pilot

from fnd.config import Config, load
from fnd.index import build_index
from fnd.tui import FNDApp
from fnd.tui.preview import tuning
from fnd.tui.preview.tuning import LAZY_MOUNT_BATCH
from fnd.tui.preview.warmth import WarmState
from fnd.tui.widgets.preview_container import PreviewContainer
from tests._pilot_wait import run_search, settle, wait_until


def _write_md(p: Path, body: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")


@pytest.fixture
def cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        textwrap.dedent("""
            [[collections.notes.sources]]
            path = "/tmp/notes"
        """),
        encoding="utf-8",
    )
    monkeypatch.setattr("fnd.config.default_config_path", lambda: cfg_path)
    return load(cfg_path)


@pytest.fixture
def long_md_index(tmp_path: Path, tmp_index_dir: Path) -> Path:
    """A 120-section markdown file — large enough that one lazy-mount
    batch can't fill from the visible window to either end of the
    document. Only section 50 mentions the search term so the auto-
    load lands deterministically."""
    notes = tmp_path / "notes"
    body_lines = ["# Long Document", ""]
    for i in range(120):
        body_lines.append(f"## Section {i}")
        body_lines.append(f"Body for section {i}." + (" target keyword here." if i == 50 else ""))
        body_lines.append("")
    _write_md(notes / "long.md", "\n".join(body_lines))
    build_index(roots=[notes], index_dir=tmp_index_dir, collection="notes")
    return tmp_index_dir


async def _drain(pilot: Pilot[None], n: int = 6) -> None:
    """Drive the event loop ``n`` ticks. Uses bare ``asyncio.sleep(0)``
    via ``settle`` to avoid Textual's 30 s ``_wait_for_screen`` timeout
    under suite load. A handful of ticks is enough to flush mount
    callbacks; full settle happens via the per-test ``wait_until``."""
    await settle(pilot, ticks=max(1, n))


@pytest.mark.asyncio
async def test_scroll_below_boundary_triggers_lazy_mount(
    cfg: Config, long_md_index: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Scrolling close to the bottom of the mounted region mounts more
    chunks below it. Disable the active-file full-mount so the file stays
    windowed — scroll-driven lazy-mount is the path for files beyond the
    full-mount budget (monster docs); for in-budget files the eager fill
    has already mounted everything below. Coverage is disabled too, so the
    chunks this mounts are built here rather than served from a capture."""
    monkeypatch.setattr("fnd.tui.preview.tuning.FULLMOUNT_CHUNK_BUDGET", 0)
    monkeypatch.setattr("fnd.tui.preview.tuning.COVERAGE_CHUNK_BUDGET", 0)
    app = FNDApp(index_dir=long_md_index, config=cfg, collection="notes")
    async with app.run_test() as pilot:
        await pilot.pause()
        await run_search(pilot, app, "target")
        pane = app.query_one("#preview_pane", VerticalScroll)
        # Wait for the initial mount AND the focus-chunk scroll to land.
        await wait_until(
            pilot,
            lambda: (
                app._preview.active is not None
                and bool(app._preview.active.mounted_indices)
                and pane.scroll_y > 0
            ),
            timeout=15.0,
            message="initial mount + focus scroll never landed",
        )
        container = app._preview.active
        assert container is not None
        mounted_before = set(container.mounted_indices)
        max_before = max(mounted_before)
        assert max_before < container.total_chunks - 1, "test setup needs unmounted below"
        target = max(0, pane.virtual_size.height - pane.size.height)
        # release() models a focused user scroll taking control — one of the two
        # ways the gate opens (the other is the nav settling, covered by
        # test_lazy_mount_fires_after_settle_without_explicit_release).
        app._preview_scroll.release()
        if pane.scroll_y == target:
            pane.scroll_to(y=max(0, target - 1), animate=False, immediate=True)
        pane.scroll_to(y=target, animate=False, immediate=True)
        # Belt and braces: the watcher → debounce → check chain has
        # several async hops that can be lost under load.
        # ``_check_preview_lazy_mount`` is idempotent and reads the
        # current scroll position directly.
        app._lazy.check()
        await wait_until(
            pilot,
            lambda: any(i > max_before for i in (container.mounted_indices - mounted_before)),
            timeout=15.0,
            message=f"no new chunks mounted below max_before={max_before}; "
            f"mounted={sorted(container.mounted_indices)}",
        )
        mounted_after = set(container.mounted_indices)
        new_below = {i for i in mounted_after - mounted_before if i > max_before}
        # Batch size is bounded — not unbounded fill.
        assert len(new_below) <= LAZY_MOUNT_BATCH * 4


@pytest.mark.asyncio
async def test_lazy_mount_fires_after_settle_without_explicit_release(
    cfg: Config, long_md_index: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: an unfocused scroll (mouse-wheel hover) must still lazy-mount.

    The dead-end bug gated lazy-mount on the anchor being armed; the anchor
    stays armed across navs and only a *focused* user scroll called release(),
    so wheel-scrolling the unfocused pane never extended the window. Now the
    gate is is_settling — it clears when the nav's scroll commits — so this
    test does NOT call release() and lazy-mount must still fire.

    Full-mount disabled so the file stays windowed (the >budget monster-file
    path); in-budget files eagerly fill below, leaving nothing to lazy-mount.
    Coverage disabled so the mounted region grows only through lazy mount.
    """
    monkeypatch.setattr("fnd.tui.preview.tuning.FULLMOUNT_CHUNK_BUDGET", 0)
    monkeypatch.setattr("fnd.tui.preview.tuning.COVERAGE_CHUNK_BUDGET", 0)
    app = FNDApp(index_dir=long_md_index, config=cfg, collection="notes")
    async with app.run_test() as pilot:
        await pilot.pause()
        await run_search(pilot, app, "target")
        pane = app.query_one("#preview_pane", VerticalScroll)
        await wait_until(
            pilot,
            lambda: (
                app._preview.active is not None
                and bool(app._preview.active.mounted_indices)
                and pane.scroll_y > 0
            ),
            timeout=15.0,
            message="initial mount + focus scroll never landed",
        )
        # The nav has landed: the gate must already be open, with NO release().
        assert not app._preview_scroll.is_settling, (
            "nav settled but is_settling still True — lazy-mount would dead-end"
        )
        container = app._preview.active
        assert container is not None
        mounted_before = set(container.mounted_indices)
        max_before = max(mounted_before)
        assert max_before < container.total_chunks - 1, "test setup needs unmounted below"
        target = max(0, pane.virtual_size.height - pane.size.height)
        if pane.scroll_y == target:
            pane.scroll_to(y=max(0, target - 1), animate=False, immediate=True)
        pane.scroll_to(y=target, animate=False, immediate=True)
        app._lazy.check()
        await wait_until(
            pilot,
            lambda: any(i > max_before for i in (container.mounted_indices - mounted_before)),
            timeout=15.0,
            message=f"no new chunks mounted below max_before={max_before} without release(); "
            f"mounted={sorted(container.mounted_indices)}",
        )


@pytest.mark.asyncio
async def test_scroll_above_after_settled_triggers_lazy_mount(
    cfg: Config, long_md_index: Path
) -> None:
    """After the initial mount has finished and the user explicitly
    scrolls up toward the top of the mounted region, more chunks above
    that region are mounted on demand."""
    app = FNDApp(index_dir=long_md_index, config=cfg, collection="notes")
    async with app.run_test() as pilot:
        await pilot.pause()
        app._search.run("target")
        # Wait for initial mount AND for the focus-chunk scroll to land.
        # If we proceed before scroll lands, ``pane.scroll_to(y=0)``
        # below is a no-op because the pane is already at y=0.
        pane = app.query_one("#preview_pane", VerticalScroll)
        await wait_until(
            pilot,
            lambda: (
                app._preview.active is not None
                and bool(app._preview.active.mounted_indices)
                and pane.scroll_y > 0
            ),
            timeout=15.0,
            message="initial mount + focus scroll never landed",
        )
        container = app._preview.active
        assert container is not None
        # Top hit is chunk 50 (only section that mentions "target keyword").
        # Initial window is focus ± 7 = 43..57. min_mounted should be 43.
        mounted_initial = set(container.mounted_indices)
        min_initial = min(mounted_initial)
        assert min_initial > 0, (
            f"test setup needs unmounted above; mounted={sorted(mounted_initial)}"
        )
        # Force scroll to the absolute top of the mounted region — that
        # puts scroll_y inside the trigger margin so the watcher fires.
        # release() models a focused user scroll taking control (one of the two
        # ways the is_settling gate opens).
        app._preview_scroll.release()
        if pane.scroll_y == 0:
            pane.scroll_to(y=1, animate=False, immediate=True)
        pane.scroll_to(y=0, animate=False, immediate=True)
        app._lazy.check()
        await wait_until(
            pilot,
            lambda: any(i < min_initial for i in (container.mounted_indices - mounted_initial)),
            timeout=15.0,
            message=f"no new chunks mounted above min_initial={min_initial}; "
            f"mounted={sorted(container.mounted_indices)}",
        )


@pytest.mark.asyncio
async def test_far_in_file_nav_lands_in_fresh_contiguous_region(
    cfg: Config, long_md_index: Path
) -> None:
    """Navigating to a far-apart match in the same file lands on the new
    match in a FRESH, contiguous mounted region — not a second window
    prepended above the current one. The old same-container resume left a
    gap between two regions and, on an upward jump, slid the visible content
    (the reflow). A fresh container is built below the outgoing one and
    swapped in, so the view never shifts and the result is one contiguous
    window. Scrolling past it still lazy-mounts (the scroll-boundary tests
    above cover that)."""
    app = FNDApp(index_dir=long_md_index, config=cfg, collection="notes")
    async with app.run_test() as pilot:
        await pilot.pause()
        await run_search(pilot, app, "target")
        await wait_until(
            pilot,
            lambda: app._preview.active is not None and bool(app._preview.active.mounted_indices),
            message="active preview never mounted any chunks",
        )
        # Auto-load focuses on section 50. Jump far UP to section 10 — the
        # case that used to prepend a window above the view and reflow.
        group = app._search.groups[0]
        app._preview.render_full_doc(group.parent_id, focus_chunk_seq=10)
        await wait_until(
            pilot,
            lambda: (
                app._preview.active is not None
                and app._preview.active.parent_doc_id == group.parent_id
                and 10 in app._preview.active.chunk_widgets
            ),
            timeout=15.0,
            message="far in-file nav never landed on the new focus chunk",
        )
        container = app._preview.active
        assert container is not None
        from itertools import pairwise

        sorted_idx = sorted(container.mounted_indices)
        # Fresh container ⇒ one contiguous window around the new focus, with
        # no gap to a stale far region (the gap was the reflow's source).
        assert all(b == a + 1 for a, b in pairwise(sorted_idx)), (
            f"expected a contiguous region after a far jump; mounted={sorted_idx}"
        )


@pytest.mark.asyncio
async def test_scroll_watcher_bridges_to_lazy_mounter(
    cfg: Config, long_md_index: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``MatchAwareScroll.watch_scroll_y`` must actually reach the
    LazyMounter. The app-level delegation shim was removed and this
    watcher kept calling the dropped name, silently severing scroll-
    driven lazy mount — undetected because the other tests drive
    ``app._lazy.check()`` directly. This test exercises ONLY the bridge:
    a real scroll on the focused pane must invoke ``schedule_check``."""
    monkeypatch.setattr("fnd.tui.preview.tuning.FULLMOUNT_CHUNK_BUDGET", 0)
    monkeypatch.setattr("fnd.tui.preview.tuning.COVERAGE_CHUNK_BUDGET", 0)
    app = FNDApp(index_dir=long_md_index, config=cfg, collection="notes")
    async with app.run_test() as pilot:
        await pilot.pause()
        await run_search(pilot, app, "target")
        pane = app.query_one("#preview_pane", VerticalScroll)
        await wait_until(
            pilot,
            lambda: app._preview.active is not None and pane.scroll_y > 0,
            timeout=15.0,
            message="initial mount never landed",
        )
        calls: list[bool] = []
        orig = app._lazy.schedule_check

        def _spy(*, user_initiated: bool = False) -> None:
            calls.append(user_initiated)
            orig(user_initiated=user_initiated)

        monkeypatch.setattr(app._lazy, "schedule_check", _spy)
        pane.focus()
        await pilot.pause()
        # A focused user scroll must bridge through to the mounter with
        # user_initiated=True — NOT a direct check() call.
        pane.scroll_to(y=pane.scroll_y + 5, animate=False, immediate=True)
        await pilot.pause()
        assert calls, "watch_scroll_y never reached LazyMounter.schedule_check"
        assert any(calls), "bridge fired but never as a user-initiated (focused) scroll"


@pytest.mark.asyncio
async def test_file_switch_cancels_lazy_mount(
    cfg: Config, tmp_path: Path, tmp_index_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pending lazy-mount task is dropped when the user navigates to
    a different file — no stray mounts on the previous container."""
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        textwrap.dedent("""
            [[collections.notes.sources]]
            path = "/tmp/notes"
        """),
        encoding="utf-8",
    )
    monkeypatch.setattr("fnd.config.default_config_path", lambda: cfg_path)
    cfg = load(cfg_path)
    notes = tmp_path / "notes"
    body_lines = ["# Doc A", ""]
    for i in range(60):
        body_lines += [f"## Section {i}", f"Body for section {i} target.", ""]
    _write_md(notes / "a.md", "\n".join(body_lines))
    _write_md(notes / "b.md", "# Doc B\n\ntarget\n")
    build_index(roots=[notes], index_dir=tmp_index_dir, collection="notes")
    app = FNDApp(index_dir=tmp_index_dir, config=cfg, collection="notes")
    async with app.run_test() as pilot:
        await pilot.pause()
        await run_search(pilot, app, "target")
        await _drain(pilot, 8)
        a = next(g for g in app._search.groups if g.path.endswith("a.md"))
        b = next(g for g in app._search.groups if g.path.endswith("b.md"))
        app._preview.render_full_doc(a.parent_id, focus_chunk_seq=5)
        await _drain(pilot, 6)
        # Switch files mid-stream.
        app._preview.render_full_doc(b.parent_id, focus_chunk_seq=0)
        await _drain(pilot, 6)
        task = app._lazy.task
        if task is not None:
            assert task.done()  # type: ignore[attr-defined]


def _batch_settled(app: FNDApp) -> bool:
    """Whether the lazy mounter has no batch in flight, as the mounter checks it."""
    task = app._lazy.task
    return task is None or bool(task.done())  # type: ignore[attr-defined]


async def _settled_preview(
    app: FNDApp, pilot: Pilot[None]
) -> tuple[VerticalScroll, PreviewContainer]:
    """Run the search and wait for the initial windowed mount to land."""
    app._search.run("target")
    pane = app.query_one("#preview_pane", VerticalScroll)
    await wait_until(
        pilot,
        lambda: (
            app._preview.active is not None
            and bool(app._preview.active.mounted_indices)
            and pane.scroll_y > 0
        ),
        timeout=15.0,
        message="initial mount + focus scroll never landed",
    )
    container = app._preview.active
    assert container is not None, "the preview never became active"
    return pane, container


@pytest.mark.asyncio
async def test_a_fully_warmed_file_fills_downward_and_never_prepends_unattended(
    cfg: Config, long_md_index: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A warmed file fills the rest of itself BELOW the reader, never above.

    Content inserted above the viewport shoves the reader down and the scroll
    can only re-pin it a layout later, so an unattended above-fill jitters for
    as long as it runs — measured at 19 of 21 painted frames showing the wrong
    part of the document. Upward stays user-driven, one batch per scroll.
    """
    app = FNDApp(index_dir=long_md_index, config=cfg, collection="notes")
    async with app.run_test() as pilot:
        await pilot.pause()
        _pane, container = await _settled_preview(app, pilot)
        chunks = app._preview.chunk_cache.get(container.parent_doc_id) or []
        assert len(container.mounted_indices) < len(chunks), "setup: needs a windowed mount"

        monkeypatch.setattr(
            type(app._preview), "file_warm_state", lambda _self, _pid: WarmState.FULL
        )
        top_before = min(container.mounted_indices)
        assert top_before > 0, "setup: needs unmounted chunks above the reader"
        app._preview_scroll.release()
        app._lazy.check()
        await settle(pilot)
        for _ in range(3):
            await settle(pilot)

        assert min(container.mounted_indices) == top_before, (
            f"the fill prepended above the reader unattended ({top_before} -> "
            f"{min(container.mounted_indices)}), which shoves the viewport down "
            f"until the scroll re-pins it a layout later"
        )
        assert len(container.mounted_indices) < len(chunks), (
            "the whole file was mounted without the reader asking for it"
        )


@pytest.mark.asyncio
async def test_a_reader_scrolling_up_is_served_while_the_fill_runs(
    cfg: Config, long_md_index: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The background fill must not wall the reader for its own duration.

    It runs downward for seconds — 3.6s on a 727-chunk file — and upward is
    reachable only on demand, so standing the reader down until it finishes is
    the wall the windowed mount was built to remove.
    """
    app = FNDApp(index_dir=long_md_index, config=cfg, collection="notes")
    async with app.run_test() as pilot:
        await pilot.pause()
        pane, container = await _settled_preview(app, pilot)
        monkeypatch.setattr(
            type(app._preview), "file_warm_state", lambda _self, _pid: WarmState.FULL
        )
        min_initial = min(container.mounted_indices)
        assert min_initial > 0, "setup: needs unmounted chunks above the reader"

        fill = asyncio.create_task(asyncio.sleep(30))
        app._lazy.fill_task = fill
        try:
            app._preview_scroll.release()
            if pane.scroll_y == 0:
                pane.scroll_to(y=1, animate=False, immediate=True)
            pane.scroll_to(y=0, animate=False, immediate=True)
            app._lazy.check()
            await wait_until(
                pilot,
                lambda: min(container.mounted_indices) < min_initial,
                timeout=15.0,
                message=f"the in-flight fill walled the scroll up from {min_initial}",
            )
        finally:
            fill.cancel()


@pytest.mark.asyncio
async def test_a_warmed_file_over_the_mount_ceiling_stays_windowed(
    cfg: Config, long_md_index: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A mounted frozen chunk costs arrange whether or not it is on screen."""
    app = FNDApp(index_dir=long_md_index, config=cfg, collection="notes")
    async with app.run_test() as pilot:
        await pilot.pause()
        pane, container = await _settled_preview(app, pilot)
        assert container is not None
        chunks = app._preview.chunk_cache.get(container.parent_doc_id) or []
        monkeypatch.setattr(
            type(app._preview), "file_warm_state", lambda _self, _pid: WarmState.FULL
        )
        monkeypatch.setattr(tuning, "FULLWARM_MOUNT_MAX_CHUNKS", 1)
        before = set(container.mounted_indices)
        app._preview_scroll.release()
        pane.scroll_to(y=0, animate=False, immediate=True)
        app._lazy.check()
        # Gate on the batch this check spawned, not a tick count: a fixed wait
        # degrades to a no-op under load and would pass without the ceiling.
        await wait_until(
            pilot,
            lambda: _batch_settled(app),
            timeout=15.0,
            message="the windowed batch never finished",
        )
        assert len(container.mounted_indices) < len(chunks), (
            "the ceiling did not hold: a file over it mounted whole"
        )
        # Only meaningful if a batch actually ran — if the scroll left the
        # viewport inside the mounted region no batch is spawned and a size
        # assertion would hold trivially.
        grew = len(container.mounted_indices) - len(before)
        assert grew == 0 or grew <= LAZY_MOUNT_BATCH, (
            f"over the ceiling a windowed batch should stay small, grew by {grew}"
        )


@pytest.mark.asyncio
async def test_the_completion_mount_refuses_while_a_navigation_is_settling(
    cfg: Config, long_md_index: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A warm finishing inside a landing must not start a large prepend.

    The scroll-driven path refuses while the controller still owns the
    position; the completion path reached `fill_all` directly and did not.
    """
    app = FNDApp(index_dir=long_md_index, config=cfg, collection="notes")
    async with app.run_test() as pilot:
        await pilot.pause()
        _pane, container = await _settled_preview(app, pilot)
        chunks = app._preview.chunk_cache.get(container.parent_doc_id) or []
        app._lazy.fill_task = None
        monkeypatch.setattr(type(app._preview_scroll), "is_settling", property(lambda _self: True))
        app._preview.mount_warmed_file(container.parent_doc_id, chunks)
        assert app._lazy.fill_task is None, (
            "the completion mount started a prepend underneath a settling navigation"
        )


@pytest.mark.asyncio
async def test_the_completion_mount_refuses_over_the_ceiling(
    cfg: Config, long_md_index: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mounted frozen chunks cost arrange whether or not they are on screen."""
    app = FNDApp(index_dir=long_md_index, config=cfg, collection="notes")
    async with app.run_test() as pilot:
        await pilot.pause()
        _pane, container = await _settled_preview(app, pilot)
        chunks = app._preview.chunk_cache.get(container.parent_doc_id) or []
        assert chunks, "setup: needs a decoded file"
        app._lazy.fill_task = None
        monkeypatch.setattr(tuning, "FULLWARM_MOUNT_MAX_CHUNKS", 1)
        app._preview.mount_warmed_file(container.parent_doc_id, chunks)
        assert app._lazy.fill_task is None, "a file over the ceiling was mounted whole"


@pytest.mark.asyncio
async def test_the_completion_mount_uses_the_fill_handle_not_the_scroll_one(
    cfg: Config, long_md_index: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Holding `task` for a whole-file mount drops every scroll-driven batch."""
    app = FNDApp(index_dir=long_md_index, config=cfg, collection="notes")
    async with app.run_test() as pilot:
        await pilot.pause()
        _pane, container = await _settled_preview(app, pilot)
        chunks = app._preview.chunk_cache.get(container.parent_doc_id) or []
        assert chunks, "setup: needs a decoded file"
        app._lazy.task = None
        app._lazy.fill_task = None
        app._preview.mount_warmed_file(container.parent_doc_id, chunks)
        assert app._lazy.task is None, "the completion mount claimed the scroll handle"
        assert app._lazy.fill_task is not None, "the completion mount never started"
        app._lazy.fill_task.cancel()


def _stub_lazy(all_captured: bool) -> tuple[Any, Any, list[Any]]:
    """A mounter whose only live edge is whether a run is in the capture store."""
    from fnd.tui.preview.lazy_mount import LazyMounter

    lazy = cast("Any", LazyMounter.__new__(LazyMounter))
    preview = type("_P", (), {"all_captured": lambda _s, _p, _q: all_captured})()
    lazy._app = type("_A", (), {"_preview": preview})()
    container = type("_C", (), {"parent_doc_id": "A"})()
    chunks = [type("_K", (), {"chunk_seq": i})() for i in range(200)]
    return lazy, container, chunks


def test_a_run_already_in_the_store_gets_the_served_batch() -> None:
    lazy, container, chunks = _stub_lazy(all_captured=True)
    assert lazy._served_batch(container, chunks, 50, -1) == tuning.LAZY_MOUNT_BATCH_SERVED


def test_a_run_that_must_be_built_gets_the_small_batch() -> None:
    """FULL is counted over CAPTURABLE chunks, so a mostly flat-path file reads
    FULL with almost none of it in the store — and the served batch is ten times
    what a build batch can afford."""
    lazy, container, chunks = _stub_lazy(all_captured=False)
    assert lazy._served_batch(container, chunks, 50, -1) == tuning.LAZY_MOUNT_BATCH
    assert lazy._served_batch(container, chunks, 50, 1) == tuning.LAZY_MOUNT_BATCH
