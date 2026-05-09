"""UX-J: chunk-match markers painted on the preview scrollbar."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from textual.widgets import Tree

from acorn.config import Config, load
from acorn.index import build_index
from acorn.tui import AcornApp
from acorn.tui.preview_scrollbar import (
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
    app = AcornApp(index_dir=md_index, config=cfg)
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
    app = AcornApp(
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
