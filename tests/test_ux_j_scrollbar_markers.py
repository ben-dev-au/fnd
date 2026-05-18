"""UX-J: chunk-match markers painted on the preview scrollbar."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from textual.widgets import Tree

from fnd.config import Config, load
from fnd.index import build_index
from fnd.tui import FNDApp
from fnd.tui.preview_scrollbar import (
    MatchAwareScroll,
    MatchAwareScrollBar,
    MatchAwareScrollBarRender,
)


def _write_md(p: Path, body: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")


def _render_glyphs(renderer: MatchAwareScrollBarRender) -> list[str]:
    """Render the scrollbar via Rich and return per-row glyph strings."""
    from rich.console import Console
    from rich.segment import Segments

    console = Console(width=1, height=6)
    segs_renderable = next(iter(renderer.__rich_console__(console, console.options)))
    assert isinstance(segs_renderable, Segments)
    return [s.text for s in segs_renderable.segments]


def test_renderer_overlays_marker_glyph_on_match_rows() -> None:
    """Given a 6-row bar and a match map of len 6 with True at index 2,
    the renderer's output segment at that row should carry the ``▌``
    glyph (and not the default blank track glyph)."""
    renderer = MatchAwareScrollBarRender(
        virtual_size=100,
        window_size=10,
        position=0,
        thickness=1,
        vertical=True,
        style="bright_magenta on #555555",
        match_map=[False, False, True, False, False, False],
    )
    glyphs = _render_glyphs(renderer)
    assert "▌" in glyphs, glyphs


def test_renderer_blank_when_no_match_map() -> None:
    """A renderer with an empty match map should fall through to the
    parent's render — no marker glyph appears."""
    renderer = MatchAwareScrollBarRender(
        virtual_size=100,
        window_size=10,
        position=0,
        thickness=1,
        vertical=True,
        style="bright_magenta on #555555",
        match_map=[],
    )
    glyphs = _render_glyphs(renderer)
    assert "▌" not in glyphs, glyphs


def test_renderer_line_precise_maps_to_exact_cell() -> None:
    """Phase 3 contract: a 1000-line file with a single match at line
    500, painted on a 10-cell track, places exactly one marker — at
    cell 5 (mid). The chunk-uniform path can't pin this; the line-
    precise path must."""
    renderer = MatchAwareScrollBarRender(
        virtual_size=1000,
        window_size=20,
        position=0,
        thickness=1,
        vertical=True,
        style="bright_magenta on #555555",
        match_lines=[500],
        total_lines=1000,
    )
    glyphs = _render_glyphs(renderer)
    # 6-cell tall console (per ``_render_glyphs``); the single match line
    # at position 500/1000 maps to cell int(500 * 6 / 1000) = 3.
    assert glyphs.count("▌") == 1, glyphs
    assert glyphs[3] == "▌", glyphs


def test_renderer_line_precise_handles_extremes() -> None:
    """Matches at line 0 and the last line of a buffer land on the
    first and last track cells respectively, never out of bounds.

    Tested at the ``_marker_cells`` layer because the thumb at the top
    of the bar occludes cell 0 in ``__rich_console__`` output — that's
    a thumb-clipping property of the parent ScrollBarRender, not a
    line-precise mapping property, so we isolate the latter here.
    """
    renderer = MatchAwareScrollBarRender(
        virtual_size=600,
        window_size=10,
        position=0,
        thickness=1,
        vertical=True,
        style="bright_magenta on #555555",
        match_lines=[0, 599],
        total_lines=600,
    )
    assert renderer._marker_cells(size=6) == {0, 5}
    # Also check a different track height — markers stay within range.
    cells = renderer._marker_cells(size=20)
    assert cells == {0, 19}


def test_renderer_prefers_line_precise_over_chunk_map() -> None:
    """When both fields are populated, the line-precise mapping wins
    — match_map is ignored. Lets callers migrate piecemeal without
    silently double-counting markers."""
    renderer = MatchAwareScrollBarRender(
        virtual_size=600,
        window_size=10,
        position=0,
        thickness=1,
        vertical=True,
        style="bright_magenta on #555555",
        # Chunk map says "every chunk matches"; line-precise says only one.
        match_map=[True] * 6,
        match_lines=[0],
        total_lines=600,
    )
    assert renderer._marker_cells(size=6) == {0}


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
def md_index(tmp_path: Path, tmp_index_dir: Path) -> Path:
    a = tmp_path / "notes"
    body = "\n".join(
        [
            "# Notes",
            "",
            "## With match",
            "the magic anchor word: glimmer is here.",
            "",
            "## Without match",
            "this section says nothing relevant.",
            "",
            "## Also matches",
            "more content with glimmer in it.",
        ]
    )
    _write_md(a / "Notes.md", body)
    build_index(roots=[a], index_dir=tmp_index_dir, collection="notes")
    return tmp_index_dir


@pytest.mark.asyncio
async def test_preview_pane_uses_match_aware_scroll(cfg: Config, md_index: Path) -> None:
    app = FNDApp(index_dir=md_index, config=cfg)
    async with app.run_test() as pilot:
        await pilot.pause()
        pane = app.query_one("#preview_pane", MatchAwareScroll)
        # The scrollbar is created lazily; touch the property and confirm
        # the type so we know our subclass is in use.
        assert isinstance(pane.vertical_scrollbar, MatchAwareScrollBar)


@pytest.mark.asyncio
async def test_match_map_propagates_to_scrollbar(cfg: Config, md_index: Path) -> None:
    """After a match-bearing query renders chunks, the scrollbar's
    ``_match_map`` should reflect which chunks contain matches."""
    app = FNDApp(
        index_dir=md_index,
        config=cfg,
        collection="notes",
        initial_query="glimmer",
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        tree = app.query_one("#results_pane", Tree)
        tree.focus()
        await pilot.pause()
        await pilot.press("down")
        await pilot.pause()
        await pilot.press("up")
        await pilot.pause()
        bar = app.query_one("#preview_pane", MatchAwareScroll).vertical_scrollbar
        assert isinstance(bar, MatchAwareScrollBar)
        # At least one chunk should be flagged as match-bearing for
        # ``glimmer`` in the fixture corpus.
        assert any(bar._match_map), bar._match_map


def test_set_match_lines_clears_chunk_map_and_vice_versa() -> None:
    """The two marker sources are mutually exclusive on the widget so
    the renderer's mode selection is unambiguous."""
    bar = MatchAwareScrollBar(vertical=True)
    bar.set_match_map([True, False, True])
    assert bar._match_map == [True, False, True]
    assert bar._match_lines == []

    bar.set_match_lines([10, 200, 999], total_lines=1000)
    assert bar._match_lines == [10, 200, 999]
    assert bar._total_lines == 1000
    assert bar._match_map == []

    bar.set_match_map([False, True])
    assert bar._match_map == [False, True]
    assert bar._match_lines == []
    assert bar._total_lines == 0
