"""A preview being staged must not move the scrollbar of the one on screen.

A container built behind ``-pre-reveal`` is in the layout — that is how the pane
scrolls to its match before it is revealed — so its rows land in the pane's
``virtual_size`` and, uncorrected, in the bar's. The thumb then shrinks for the
two frames a swap takes and snaps back: a blink in the gutter on every
navigation.
"""

from __future__ import annotations

import pytest
from rich.console import Console
from rich.segment import Segments
from textual.app import App, ComposeResult
from textual.widgets import Static

from fnd.tui.preview_scrollbar import _THUMB_GLYPH, MatchAwareScroll, MatchAwareScrollBar

_HEIGHT = 24


class _PaneApp(App[None]):
    def compose(self) -> ComposeResult:
        with MatchAwareScroll(id="preview_pane"):
            yield Static("document", id="doc")


def _thumb_cells(pane: MatchAwareScroll) -> int:
    """Cells the bar actually paints as thumb — the glyph, not the model."""
    bar = pane.vertical_scrollbar
    assert isinstance(bar, MatchAwareScrollBar)
    renderer = bar._render_bar("bright_magenta on #555555")
    console = Console(width=1, height=_HEIGHT)
    segments = next(iter(renderer.__rich_console__(console, console.options)))
    assert isinstance(segments, Segments)
    return sum(1 for s in segments.segments if s.text == _THUMB_GLYPH)


@pytest.mark.asyncio
async def test_a_staged_container_does_not_resize_the_thumb() -> None:
    app = _PaneApp()
    async with app.run_test(size=(80, _HEIGHT)) as pilot:
        pane = app.query_one("#preview_pane", MatchAwareScroll)
        app.query_one("#doc", Static).styles.height = 100
        await pilot.pause()
        before = _thumb_cells(pane)
        assert before > 1, f"setup: a {before}-cell thumb cannot shrink measurably"

        staged = Static("staged", classes="-pre-reveal")
        staged.styles.height = 100
        await pane.mount(staged)
        await pilot.pause()

        assert pane.staged_rows == 100, (
            f"setup: the pane counted {pane.staged_rows} staged rows, so the bar was "
            f"never asked to ignore anything"
        )
        assert pane.vertical_scrollbar.window_virtual_size == 200, (
            "setup: the staged container never reached the bar's own model, so "
            "correcting it proves nothing"
        )
        assert _thumb_cells(pane) == before, (
            f"the thumb went {before} cells -> {_thumb_cells(pane)} while a container "
            f"the reader cannot see was staged in the layout"
        )
