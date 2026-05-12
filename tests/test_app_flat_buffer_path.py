"""Phase 5 host wire-in — AcornApp routes PDF/TXT through LineBufferPreview.

These tests assert the user-visible contracts the redesign promised:

* The active widget for a PDF is a LineBufferPreview, not a tree of
  per-line Static widgets. Steady-state DOM under the preview pane is
  O(1) widgets per file regardless of file size — the whole reason for
  the redesign.
* The buffer's own scrollbar (MatchAwareScrollBar) is fed line-precise
  match positions automatically on ``set_file_view`` — the host doesn't
  call ``set_match_map`` for flat files.
* Cache hits flip ``-hidden`` instead of remounting (no rebuild cost).
* Switching to a markdown file falls back to the structural pipeline
  unchanged (the existing PreviewContainer path).
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from textual.widgets import Static, Tree

from acorn.config import Config, load
from acorn.index import build_index
from acorn.tui import AcornApp
from acorn.tui.line_buffer import LineBufferPreview
from acorn.tui.preview_scrollbar import MatchAwareScrollBar


@pytest.fixture
def cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        textwrap.dedent("""
            [[collections.default.sources]]
            path = "/tmp/notes"
        """),
        encoding="utf-8",
    )
    monkeypatch.setattr("acorn.config.default_config_path", lambda: cfg_path)
    return load(cfg_path)


@pytest.fixture
def pdf_index(fixtures_dir: Path, tmp_index_dir: Path) -> Path:
    build_index(roots=[fixtures_dir], index_dir=tmp_index_dir, collection="default")
    return tmp_index_dir


@pytest.fixture
def md_index(tmp_path: Path, tmp_index_dir: Path) -> Path:
    notes = tmp_path / "notes"
    notes.mkdir()
    body = "\n".join(
        [
            "# Heading",
            "",
            "## Section A",
            "anchor susy line one",
            "",
            "## Section B",
            "anchor susy line two",
        ]
    )
    (notes / "n.md").write_text(body, encoding="utf-8")
    build_index(roots=[notes], index_dir=tmp_index_dir, collection="default")
    return tmp_index_dir


# ── PDF takes the flat-buffer path ─────────────────────────────────


@pytest.mark.asyncio
async def test_pdf_preview_uses_line_buffer(pdf_index: Path) -> None:
    """A PDF result mounts one LineBufferPreview inside #preview_pane;
    no PreviewContainer is active."""
    app = AcornApp(index_dir=pdf_index, initial_query="blue penguin sandwich")
    async with app.run_test() as pilot:
        await pilot.pause()
        tree = app.query_one("#results_pane", Tree)
        first = next(iter(tree.root.children))
        first.expand()
        await pilot.pause()
        tree.focus()
        await pilot.press("down")
        await pilot.pause()
        assert app._active_flat_buffer is not None
        assert app._active_preview is None
        pane = app.query_one("#preview_pane")
        # Exactly one LineBufferPreview lives inside the pane.
        buffers = list(pane.query(LineBufferPreview))
        assert len(buffers) == 1
        assert buffers[0] is app._active_flat_buffer


@pytest.mark.asyncio
async def test_flat_buffer_scrollbar_carries_line_precise_markers(
    pdf_index: Path,
) -> None:
    """The widget pushes its match-line positions into its own
    MatchAwareScrollBar on ``set_file_view`` so the line-precise
    markers paint without any wiring from the host."""
    app = AcornApp(index_dir=pdf_index, initial_query="blue penguin sandwich")
    async with app.run_test() as pilot:
        await pilot.pause()
        tree = app.query_one("#results_pane", Tree)
        first = next(iter(tree.root.children))
        first.expand()
        await pilot.pause()
        tree.focus()
        await pilot.press("down")
        await pilot.pause()
        buf = app._active_flat_buffer
        assert buf is not None
        bar = buf.vertical_scrollbar
        assert isinstance(bar, MatchAwareScrollBar)
        # Line-precise data populated; legacy chunk-uniform data left
        # empty (the buffer never calls set_match_map).
        assert bar._match_lines, bar._match_lines
        assert bar._total_lines > 0
        assert bar._match_map == []
        # The pushed match lines match what the buffer reports publicly.
        assert bar._match_lines == buf.match_lines


@pytest.mark.asyncio
async def test_flat_buffer_cache_hit_reuses_widget(pdf_index: Path) -> None:
    """Re-cursoring back onto the same PDF doesn't remount — the
    cached LineBufferPreview is flipped visible again."""
    app = AcornApp(index_dir=pdf_index, initial_query="blue penguin sandwich")
    async with app.run_test() as pilot:
        await pilot.pause()
        tree = app.query_one("#results_pane", Tree)
        first = next(iter(tree.root.children))
        first.expand()
        await pilot.pause()
        tree.focus()
        await pilot.press("down")
        await pilot.pause()
        buf_first_visit = app._active_flat_buffer
        assert buf_first_visit is not None
        # Move the cursor away then back to the same file.
        await pilot.press("up")
        await pilot.pause()
        await pilot.press("down")
        await pilot.pause()
        assert (
            app._active_flat_buffer is buf_first_visit
        ), "cache hit should reuse the existing buffer, not remount a fresh one"


# ── Markdown stays on the structural path ──────────────────────────


@pytest.mark.asyncio
async def test_md_preview_keeps_structural_path(cfg: Config, md_index: Path) -> None:
    """Markdown files still mount the PreviewContainer — the structural
    path is unchanged by the Phase 5 wire-in."""
    app = AcornApp(index_dir=md_index, config=cfg, initial_query="susy")
    async with app.run_test() as pilot:
        await pilot.pause()
        tree = app.query_one("#results_pane", Tree)
        first = next(iter(tree.root.children))
        first.expand()
        await pilot.pause()
        tree.focus()
        await pilot.press("down")
        await pilot.pause()
        assert app._active_preview is not None
        assert app._active_flat_buffer is None
        # The pane carries the placeholder OR a PreviewContainer
        # (not a LineBufferPreview).
        pane = app.query_one("#preview_pane")
        buffers = list(pane.query(LineBufferPreview))
        assert buffers == []
        # Placeholder Static is gone once a container mounted.
        statics = [s for s in pane.query(Static) if "placeholder" in s.classes]
        assert statics == []
