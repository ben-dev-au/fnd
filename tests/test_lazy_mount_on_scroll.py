"""Scroll-driven lazy mount: long files mount more chunks as the user
scrolls toward the boundary of the currently-mounted region.

Before this path existed, the structural preview was locked to a fixed
focus ± _VISIBLE_FIRST_* window; scrolling past it just hit a wall. Now
``MatchAwareScroll.watch_scroll_y`` notifies the app and the app mounts
the next ``_LAZY_MOUNT_BATCH`` chunks in the scroll direction.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from textual.containers import VerticalScroll
from textual.pilot import Pilot

from fnd.config import Config, load
from fnd.index import build_index
from fnd.tui import FNDApp
from fnd.tui.app import _LAZY_MOUNT_BATCH


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
    for _ in range(n):
        await pilot.pause()


async def _drain_secs(pilot: Pilot[None], seconds: float) -> None:
    """Wall-clock drain + idle settle for the lazy-mount debounce timer
    (``set_timer`` uses real seconds, not pilot tick count). One big
    ``pilot.pause(seconds)`` flaked under full-suite load — splitting
    the budget into slices and idle-draining between each gives the
    event loop room to flush the watcher → debounce → mount chain."""
    slices = max(4, int(seconds * 20))
    per_slice = seconds / slices
    for _ in range(slices):
        await pilot.pause(per_slice)
        await pilot.pause()


@pytest.mark.asyncio
async def test_scroll_below_boundary_triggers_lazy_mount(cfg: Config, long_md_index: Path) -> None:
    """Scrolling close to the bottom of the mounted region mounts more
    chunks below it."""
    app = FNDApp(index_dir=long_md_index, config=cfg, collection="notes")
    async with app.run_test() as pilot:
        await pilot.pause()
        app._run_query("target")
        await _drain(pilot, 12)
        container = app._active_preview
        assert container is not None
        mounted_before = set(container.mounted_indices)
        max_before = max(mounted_before)
        assert max_before < container.total_chunks - 1, "test setup needs unmounted below"
        pane = app.query_one("#preview_pane", VerticalScroll)
        target = max(0, pane.virtual_size.height - pane.size.height)
        pane.scroll_to(y=target, animate=False, immediate=True)
        await _drain_secs(pilot, 0.5)
        await _drain(pilot, 10)
        mounted_after = set(container.mounted_indices)
        new_below = {i for i in mounted_after - mounted_before if i > max_before}
        assert new_below, (
            f"expected new chunks mounted below; before={sorted(mounted_before)} "
            f"after={sorted(mounted_after)}"
        )
        # Batch size is bounded — not unbounded fill.
        assert len(new_below) <= _LAZY_MOUNT_BATCH * 4


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
        app._run_query("target")
        await _drain(pilot, 12)
        container = app._active_preview
        assert container is not None
        # Top hit is chunk 50 (only section that mentions "target keyword").
        # Initial window is focus ± 7 = 43..57. min_mounted should be 43.
        mounted_initial = set(container.mounted_indices)
        min_initial = min(mounted_initial)
        assert (
            min_initial > 0
        ), f"test setup needs unmounted above; mounted={sorted(mounted_initial)}"
        pane = app.query_one("#preview_pane", VerticalScroll)
        # Force scroll to the absolute top of the mounted region — that
        # puts scroll_y inside the trigger margin so the watcher fires.
        pane.scroll_to(y=0, animate=False, immediate=True)
        await _drain_secs(pilot, 0.5)
        await _drain(pilot, 10)
        mounted_after = set(container.mounted_indices)
        new_above = {i for i in mounted_after - mounted_initial if i < min_initial}
        assert new_above, (
            f"expected new chunks mounted above; initial={sorted(mounted_initial)} "
            f"after={sorted(mounted_after)}"
        )


@pytest.mark.asyncio
async def test_gap_between_two_mounted_regions_fills_on_scroll(
    cfg: Config, long_md_index: Path
) -> None:
    """When the user navigates between two far-apart matches in the
    same file, the mounted region has a gap. Scrolling down within the
    earlier region (toward the gap) progressively fills it."""
    app = FNDApp(index_dir=long_md_index, config=cfg, collection="notes")
    async with app.run_test() as pilot:
        await pilot.pause()
        app._run_query("target")
        await _drain(pilot, 12)
        # Auto-load focuses on chunk 50. Now resume on a far-earlier chunk:
        # ±7 window around chunk 10 leaves a gap with chunk 50's region.
        group = app._groups[0]
        app._render_full_doc(group.parent_id, focus_chunk_seq=10)
        await _drain(pilot, 12)
        container = app._active_preview
        assert container is not None
        sorted_idx = sorted(container.mounted_indices)
        # Two disjoint regions, with at least one unmounted index between.
        from itertools import pairwise

        gaps = [(a, b) for a, b in pairwise(sorted_idx) if b > a + 1]
        assert gaps, f"test setup needs a gap; mounted={sorted_idx}"
        gap_lo = gaps[0][0]
        from textual.containers import VerticalScroll

        pane = app.query_one("#preview_pane", VerticalScroll)
        # Scroll to the very bottom of the lower region (chunk gap_lo's
        # widget) — that's where the gap-fill trigger should fire.
        widget = container.chunk_widgets[
            next(c for c in app._chunk_cache[group.parent_id] if c.chunk_seq == gap_lo).chunk_seq
        ]
        target_y = widget.virtual_region.y + widget.virtual_region.height - pane.size.height
        pane.scroll_to(y=max(0, target_y), animate=False, immediate=True)
        await _drain_secs(pilot, 0.5)
        await _drain(pilot, 10)
        # At least the chunk immediately after gap_lo must now be mounted.
        assert gap_lo + 1 in container.mounted_indices, (
            f"expected gap-fill to mount chunk {gap_lo + 1}; "
            f"mounted={sorted(container.mounted_indices)}"
        )


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
        app._run_query("target")
        await _drain(pilot, 8)
        a = next(g for g in app._groups if g.path.endswith("a.md"))
        b = next(g for g in app._groups if g.path.endswith("b.md"))
        app._render_full_doc(a.parent_id, focus_chunk_seq=5)
        await _drain(pilot, 6)
        # Switch files mid-stream.
        app._render_full_doc(b.parent_id, focus_chunk_seq=0)
        await _drain(pilot, 6)
        task = app._lazy_mount_task
        if task is not None:
            assert task.done()  # type: ignore[attr-defined]
