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
    _MARKER_GLYPH,
    _THUMB_GLYPH,
    _THUMB_GLYPH_HORIZONTAL,
    MatchAwareScroll,
    MatchAwareScrollBar,
    MatchAwareScrollBarRender,
    ThinScrollBarRender,
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


def _thumb_glyphs(renderer: ThinScrollBarRender, height: int = 8) -> list[str]:
    from rich.console import Console
    from rich.segment import Segments

    console = Console(width=1, height=height)
    segs = next(iter(renderer.__rich_console__(console, console.options)))
    assert isinstance(segs, Segments)
    return [s.text for s in segs.segments]


def test_thin_renderer_paints_box_vertical_thumb_not_full_block() -> None:
    """The thin renderer paints thumb cells as the box-drawing vertical
    (the pane border's glyph), as foreground over a transparent track —
    never a reverse-video full-cell block."""
    renderer = ThinScrollBarRender(
        virtual_size=100,
        window_size=20,
        position=40,
        thickness=1,
        vertical=True,
        style="bright_magenta on #555555",
    )
    from rich.console import Console
    from rich.segment import Segments

    console = Console(width=1, height=8)
    segs = next(iter(renderer.__rich_console__(console, console.options)))
    assert isinstance(segs, Segments)
    seglist = list(segs.segments)
    thumb = [s for s in seglist if s.text == "│"]
    assert thumb, [s.text for s in seglist]
    # Thumb cells are foreground glyphs, never reverse-video blocks.
    assert all(s.style is None or not s.style.reverse for s in seglist)
    # Thumb cells keep the grab meta so click-drag still works.
    assert all((s.style and s.style.meta.get("@mouse.down")) == "grab" for s in thumb)


def test_thin_thumb_size_is_constant_across_scroll_positions() -> None:
    """The thumb is a fixed number of cells regardless of scroll position
    — it must not resize as the user scrolls (only ``top`` moves)."""
    counts = set()
    for pos in range(0, 81, 5):  # 0 .. max_scroll for virtual 100, window 20
        renderer = ThinScrollBarRender(
            virtual_size=100,
            window_size=20,
            position=pos,
            thickness=1,
            vertical=True,
            style="bright_magenta on #555555",
        )
        counts.add(_thumb_glyphs(renderer, height=10).count("│"))
    assert len(counts) == 1, f"thumb resized while scrolling: cell counts {counts}"


def _horizontal_glyphs(renderer: ThinScrollBarRender, width: int = 20) -> str:
    from rich.console import Console
    from rich.segment import Segments

    console = Console(width=width, height=1)
    segs = next(iter(renderer.__rich_console__(console, console.options)))
    assert isinstance(segs, Segments)
    return "".join(s.text for s in segs.segments)


_STOCK_BLOCKS = set("█▉▊▋▌▍▎▏")


def test_thin_renderer_horizontal_uses_box_glyph_not_stock_block() -> None:
    """Horizontal bars (wide tables, unwrapped code fences) must read the same
    thin weight as the vertical bar — the centralised renderer covers both
    orientations rather than falling through to stock partial-block glyphs."""
    renderer = ThinScrollBarRender(
        virtual_size=200,
        window_size=50,
        position=40,
        thickness=1,
        vertical=False,
        style="bright_magenta on #555555",
    )
    glyphs = _horizontal_glyphs(renderer)
    assert _THUMB_GLYPH_HORIZONTAL in glyphs
    assert not (set(glyphs) & _STOCK_BLOCKS), sorted(set(glyphs))


def test_thin_renderer_box_glyph_both_orientations() -> None:
    """A single centralised renderer thins both orientations: ``│`` vertical,
    ``─`` horizontal, never a stock block in either."""
    for vertical, glyph in ((True, _THUMB_GLYPH), (False, _THUMB_GLYPH_HORIZONTAL)):
        renderer = ThinScrollBarRender(
            virtual_size=200,
            window_size=50,
            position=40,
            thickness=1,
            vertical=vertical,
            style="bright_magenta on #555555",
        )
        glyphs = (
            "".join(_thumb_glyphs(renderer, height=20))
            if vertical
            else _horizontal_glyphs(renderer)
        )
        assert glyph in glyphs, (vertical, glyphs)
        assert not (set(glyphs) & _STOCK_BLOCKS), (vertical, sorted(set(glyphs)))


def test_match_aware_horizontal_has_no_markers() -> None:
    """Markers map document lines onto a vertical track; a horizontal axis has
    no line mapping, so the horizontal bar carries no marker glyph."""
    renderer = MatchAwareScrollBarRender(
        virtual_size=200,
        window_size=50,
        position=40,
        thickness=1,
        vertical=False,
        style="bright_magenta on #555555",
        match_map=[True, True, True],
    )
    assert _MARKER_GLYPH not in _horizontal_glyphs(renderer)


def test_match_aware_inherits_thin_thumb_and_keeps_markers() -> None:
    """The preview's renderer thins the thumb like every other bar while
    still painting match markers on the track."""
    renderer = MatchAwareScrollBarRender(
        virtual_size=1000,
        window_size=20,
        position=0,
        thickness=1,
        vertical=True,
        style="bright_magenta on #555555",
        match_lines=[700],
        total_lines=1000,
    )
    glyphs = _render_glyphs(renderer)
    assert "│" in glyphs, glyphs  # thin box-vertical thumb at the top
    assert "▌" in glyphs, glyphs  # match marker on the track


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
async def test_match_positions_propagate_to_scrollbar(cfg: Config, md_index: Path) -> None:
    """With the in-development toggle on, a match-bearing query populates
    the scrollbar's line-precise marker data (placed by line position,
    not chunk ordinal)."""
    cfg.defaults.scrollbar_match_highlight = True
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
        # Markers are computed off-thread now; settle the worker before asserting.
        await app.workers.wait_for_complete()
        await pilot.pause()
        bar = app.query_one("#preview_pane", MatchAwareScroll).vertical_scrollbar
        assert isinstance(bar, MatchAwareScrollBar)
        # ``glimmer`` matches in the fixture, so the line-precise feed is
        # active: at least one marker line within a positive total.
        assert bar._total_lines > 0, bar._total_lines
        assert bar._match_lines, bar._match_lines
        assert all(0 <= ln < bar._total_lines for ln in bar._match_lines)
        # The legacy chunk-uniform map is no longer used.
        assert bar._match_map == []


@pytest.mark.asyncio
async def test_marker_scan_runs_off_main_thread(
    cfg: Config, md_index: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``structural_match_lines`` scans every source line and can freeze the
    UI for seconds on a large no-match document, so the scan must run on a
    worker thread, never the event loop. Capture the thread it executes on."""
    import threading

    from fnd.tui import preview_markers

    seen: dict[str, object] = {}
    real = preview_markers.structural_match_lines

    def _spy(chunks: object, spec: object) -> object:
        seen["thread"] = threading.current_thread()
        return real(chunks, spec)  # type: ignore[arg-type]

    monkeypatch.setattr(preview_markers, "structural_match_lines", _spy)

    cfg.defaults.scrollbar_match_highlight = True
    app = FNDApp(index_dir=md_index, config=cfg, collection="notes", initial_query="glimmer")
    async with app.run_test() as pilot:
        await pilot.pause()
        tree = app.query_one("#results_pane", Tree)
        tree.focus()
        await pilot.pause()
        await pilot.press("down")
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert "thread" in seen, "structural_match_lines was never invoked"
        assert seen["thread"] is not threading.main_thread(), "marker scan ran on the event loop"
        # Result still lands on the bar.
        bar = app.query_one("#preview_pane", MatchAwareScroll).vertical_scrollbar
        assert isinstance(bar, MatchAwareScrollBar)
        assert bar._match_lines, bar._match_lines


@pytest.mark.asyncio
async def test_markers_off_by_default_paints_nothing(cfg: Config, md_index: Path) -> None:
    """Scrollbar match highlighting is in-development: with the toggle at
    its default (off), a matching query feeds no markers to the bar."""
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
        assert bar._match_lines == []
        assert bar._match_map == []


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
