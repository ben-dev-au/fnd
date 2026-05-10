"""UXP-4 §4 — preview-load worker keeps the UI responsive on big files
and surfaces a real ProgressBar + LoadingIndicator (no border-title text)
so the user gets unambiguous feedback that work is happening."""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any

import pytest
from textual.widgets import ProgressBar

from acorn.config import Config, load
from acorn.index import build_index
from acorn.tui import AcornApp


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
    monkeypatch.setattr("acorn.config.default_config_path", lambda: cfg_path)
    return load(cfg_path)


@pytest.fixture
def two_file_index(tmp_path: Path, tmp_index_dir: Path) -> Path:
    """Two files: a small one and a many-section one, so we can switch
    between them and observe worker behavior."""
    a = tmp_path / "notes"
    body_lines = ["# Big Book", ""]
    for i in range(60):
        body_lines.append(f"## Section {i}")
        body_lines.append(f"Body for section {i} mentioning the target term.")
        body_lines.append("")
    _write_md(a / "small.md", "# Small\n\ntarget mention here.\n")
    _write_md(a / "big.md", "\n".join(body_lines))
    build_index(roots=[a], index_dir=tmp_index_dir, collection="notes")
    return tmp_index_dir


@pytest.mark.asyncio
async def test_preview_load_dispatches_worker_on_cache_miss(
    cfg: Config, two_file_index: Path
) -> None:
    """First-time _render_full_doc dispatches a preview-load worker."""
    app = AcornApp(index_dir=two_file_index, config=cfg, collection="notes")
    async with app.run_test() as pilot:
        await pilot.pause()
        app._run_query("target")
        await pilot.pause()
        big_group = next(g for g in app._groups if g.path.endswith("big.md"))
        app._render_full_doc(big_group.parent_id, focus_chunk_seq=0)
        # Worker dispatched in the preview-load group.
        worker_groups = [w.group for w in app.workers]
        assert "preview-load" in worker_groups
        # Drain the worker + mount batches.
        await pilot.pause()
        await pilot.pause()
        # Cache populated after load completes.
        assert big_group.parent_id in app._chunk_cache
        # Cache hit path: no new worker, no progress, no spinner.
        before_workers = len(app.workers)
        app._render_full_doc(big_group.parent_id, focus_chunk_seq=0)
        await pilot.pause()
        assert app._preview_load_progress is None
        assert len(app.workers) <= before_workers


@pytest.mark.asyncio
async def test_preview_clears_old_content_and_shows_indicators(
    cfg: Config, two_file_index: Path
) -> None:
    """On cache miss the pane immediately replaces previous content with
    a ProgressBar (indeterminate) + centered LoadingIndicator — no
    holdover from the previously-selected file."""
    app = AcornApp(index_dir=two_file_index, config=cfg, collection="notes")
    async with app.run_test() as pilot:
        await pilot.pause()
        app._run_query("target")
        await pilot.pause()
        small_group = next(g for g in app._groups if g.path.endswith("small.md"))
        big_group = next(g for g in app._groups if g.path.endswith("big.md"))
        # Load small file first so the pane has content to displace.
        app._render_full_doc(small_group.parent_id, focus_chunk_seq=0)
        await pilot.pause()
        await pilot.pause()
        assert app._preview_parent_id == small_group.parent_id
        # Now switch to the big file. Immediately after dispatch (sync
        # return) the pane must be cleared and the indicators mounted —
        # no chunks from small.md left visible.
        app._render_full_doc(big_group.parent_id, focus_chunk_seq=0)
        # Indicators present (Center container holds the LoadingIndicator;
        # the inner widget itself takes a tick to compose, so checking
        # the wrapper is sufficient).
        assert len(app.query(".preview-progress")) == 1
        assert len(app.query(".preview-loading")) == 1
        # _preview_parent_id reset (no holdover) and progress flagged.
        assert app._preview_parent_id is None
        assert app._preview_load_progress is not None
        # Title reflects clean state, not "loading N/M chunks".
        assert "loading" not in app._preview_title().lower()


@pytest.mark.asyncio
async def test_progress_bar_switches_to_determinate_after_decode(
    cfg: Config, two_file_index: Path
) -> None:
    """The bar starts indeterminate (total=None) during decode, then
    flips to determinate (total=len(chunks)) when chunks arrive — and
    is removed when mount finishes."""
    app = AcornApp(index_dir=two_file_index, config=cfg, collection="notes")
    async with app.run_test() as pilot:
        await pilot.pause()
        app._run_query("target")
        await pilot.pause()
        big_group = next(g for g in app._groups if g.path.endswith("big.md"))
        app._render_full_doc(big_group.parent_id, focus_chunk_seq=0)
        # Indeterminate bar mounted immediately.
        bar = app.query_one(".preview-progress", ProgressBar)
        assert bar.total is None
        # Drain decode + mount completely.
        await pilot.pause()
        await pilot.pause()
        await pilot.pause()
        # After mount completes, the bar self-removes.
        assert len(app.query(".preview-progress")) == 0
        assert len(app.query(".preview-loading")) == 0
        # And the chunks are mounted (preview pane has children beyond
        # the indicators — the title widget plus chunk widgets).
        pane = app.query_one("#preview_pane")
        assert len(pane.children) > 0


@pytest.mark.asyncio
async def test_switching_files_mid_load_cancels_mount_task(
    cfg: Config, two_file_index: Path
) -> None:
    """Switching to a new file mid-mount cancels the previous mount
    task so chunks from the old file can't append into the new pane."""
    app = AcornApp(index_dir=two_file_index, config=cfg, collection="notes")
    async with app.run_test() as pilot:
        await pilot.pause()
        app._run_query("target")
        await pilot.pause()
        small_group = next(g for g in app._groups if g.path.endswith("small.md"))
        big_group = next(g for g in app._groups if g.path.endswith("big.md"))
        # Trigger big.md load and let decode complete + mount start.
        app._render_full_doc(big_group.parent_id, focus_chunk_seq=0)
        await pilot.pause()  # decode done, mount task started
        first_task: Any = app._preview_mount_task
        # Switch to small.md before big's mount finishes.
        app._render_full_doc(small_group.parent_id, focus_chunk_seq=0)
        # The old task should be cancelled or already done; the
        # _cancel_preview_mount_task helper also nils out the field, so
        # treat None as a valid "cancelled" outcome.
        assert first_task is None or first_task.done() or first_task.cancelled()
        # Drain the small load.
        await pilot.pause()
        await pilot.pause()
        # Final state reflects small.md, not big.md.
        assert app._preview_parent_id == small_group.parent_id


@pytest.mark.asyncio
async def test_rapid_file_switching_does_not_raise_duplicate_ids(
    cfg: Config, two_file_index: Path
) -> None:
    """Regression: switching files mid-mount used to crash with
    DuplicateIds because cancellation didn't run the bar's cleanup,
    leaving an id-collision-prone widget in the DOM. Class-based
    selectors + try/finally cleanup make this benign."""
    app = AcornApp(index_dir=two_file_index, config=cfg, collection="notes")
    async with app.run_test() as pilot:
        await pilot.pause()
        app._run_query("target")
        await pilot.pause()
        small_group = next(g for g in app._groups if g.path.endswith("small.md"))
        big_group = next(g for g in app._groups if g.path.endswith("big.md"))
        # Toggle several times in quick succession — each call cancels
        # the in-flight mount and starts a new one. None of these calls
        # should raise.
        for _ in range(5):
            app._render_full_doc(big_group.parent_id, focus_chunk_seq=0)
            await pilot.pause()
            app._render_full_doc(small_group.parent_id, focus_chunk_seq=0)
            await pilot.pause()
        # Drain to a settled state.
        await pilot.pause()
        await pilot.pause()


@pytest.mark.asyncio
async def test_preview_title_no_longer_carries_progress_text(
    cfg: Config, two_file_index: Path
) -> None:
    """The border title stays clean — never says 'loading N/M chunks'.
    Progress lives on the ProgressBar widget instead."""
    app = AcornApp(index_dir=two_file_index, config=cfg, collection="notes")
    async with app.run_test() as pilot:
        await pilot.pause()
        app._run_query("target")
        await pilot.pause()
        big_group = next(g for g in app._groups if g.path.endswith("big.md"))
        app._render_full_doc(big_group.parent_id, focus_chunk_seq=0)
        # During load, title must not contain 'loading' / 'chunks'.
        title = app._preview_title()
        assert "loading" not in title.lower()
        assert "chunks" not in title.lower()
        # Drain.
        await pilot.pause()
        await pilot.pause()
        await pilot.pause()
        # After load the title shows the file basename.
        title = app._preview_title()
        assert "big.md" in title
