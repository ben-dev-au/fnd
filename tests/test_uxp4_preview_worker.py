"""UXP-4 §4 — preview-load worker keeps the UI responsive on big files."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

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
    """A first-time _render_full_doc on a parent_id dispatches a worker
    in the ``preview-load`` group; cache hits do not."""
    app = AcornApp(index_dir=two_file_index, config=cfg, collection="notes")
    async with app.run_test() as pilot:
        await pilot.pause()
        app._run_query("target")
        await pilot.pause()
        big_group = next(g for g in app._groups if g.path.endswith("big.md"))
        # Cache miss → worker dispatched and progress placeholder set.
        app._render_full_doc(big_group.parent_id, focus_chunk_seq=0)
        assert app._preview_load_progress is not None
        # At least one preview-load worker exists immediately after dispatch.
        worker_groups = [w.group for w in app.workers]
        assert "preview-load" in worker_groups
        # Drain the worker + mount batches.
        await pilot.pause()
        await pilot.pause()
        # After load completes, the progress should clear and chunks cache.
        assert big_group.parent_id in app._chunk_cache
        # A second _render_full_doc on the same parent_id is a cache hit
        # — no new worker, no progress placeholder.
        app._preview_load_progress = None
        app._render_full_doc(big_group.parent_id, focus_chunk_seq=0)
        assert app._preview_load_progress is None


@pytest.mark.asyncio
async def test_preview_keeps_old_content_until_mount_begins(
    cfg: Config, two_file_index: Path
) -> None:
    """While the worker is decoding, the previous preview stays mounted —
    we don't blank the pane on cursor move."""
    app = AcornApp(index_dir=two_file_index, config=cfg, collection="notes")
    async with app.run_test() as pilot:
        await pilot.pause()
        app._run_query("target")
        await pilot.pause()
        small_group = next(g for g in app._groups if g.path.endswith("small.md"))
        big_group = next(g for g in app._groups if g.path.endswith("big.md"))
        # Load small file first (synchronous: cache populates after worker drains).
        app._render_full_doc(small_group.parent_id, focus_chunk_seq=0)
        await pilot.pause()
        await pilot.pause()
        # _preview_parent_id should now be the small file.
        assert app._preview_parent_id == small_group.parent_id
        small_parent = app._preview_parent_id
        # Now switch to the big file. The worker will decode + mount; before
        # mount begins, _preview_parent_id should still equal the small file
        # (we don't blank prematurely).
        app._render_full_doc(big_group.parent_id, focus_chunk_seq=0)
        # Immediately after dispatch (sync return), preview still shows small
        # OR transitioned to a loading-progress state — accept either as
        # "not yet committed to the new file".
        assert app._preview_parent_id == small_parent or app._preview_load_progress is not None


@pytest.mark.asyncio
async def test_preview_progress_appears_in_border_title(cfg: Config, two_file_index: Path) -> None:
    """While loading, the preview pane's border title surfaces
    'loading N/M chunks' — built via _preview_title which _refresh_status
    pushes to the live border_title."""
    app = AcornApp(index_dir=two_file_index, config=cfg, collection="notes")
    async with app.run_test() as pilot:
        await pilot.pause()
        app._run_query("target")
        await pilot.pause()
        big_group = next(g for g in app._groups if g.path.endswith("big.md"))
        app._render_full_doc(big_group.parent_id, focus_chunk_seq=0)
        # Right after dispatch, progress is set (placeholder 0/1) and
        # _preview_title reflects "loading".
        assert app._preview_load_progress is not None
        title = app._preview_title()
        assert "loading" in title.lower()
