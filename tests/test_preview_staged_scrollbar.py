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

from fnd.tui.preview.visibility import set_preview_visibility
from fnd.tui.preview_scrollbar import _THUMB_GLYPH, MatchAwareScroll, MatchAwareScrollBar
from fnd.tui.widgets.preview_container import PreviewContainer

_HEIGHT = 24


class _PaneApp(App[None]):
    def compose(self) -> ComposeResult:
        with MatchAwareScroll(id="preview_pane"):
            yield Static("document", id="doc")


def _thumb(pane: MatchAwareScroll) -> tuple[int | None, int]:
    """(first cell, length) of the thumb the bar actually paints."""
    bar = pane.vertical_scrollbar
    assert isinstance(bar, MatchAwareScrollBar)
    renderer = bar._render_bar("bright_magenta on #555555")
    console = Console(width=1, height=_HEIGHT)
    segments = next(iter(renderer.__rich_console__(console, console.options)))
    assert isinstance(segments, Segments)
    cells = [i for i, s in enumerate(segments.segments) if s.text == _THUMB_GLYPH]
    return (cells[0] if cells else None, len(cells))


def _thumb_cells(pane: MatchAwareScroll) -> int:
    return _thumb(pane)[1]


async def _preview(pane: MatchAwareScroll, name: str, rows: int) -> PreviewContainer:
    container = PreviewContainer(parent_doc_id=name, query_signature="q", total_chunks=1)
    body = Static(name)
    body.styles.height = rows
    await pane.mount(container)
    await container.mount(body)
    return container


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


class _EmptyPaneApp(App[None]):
    def compose(self) -> ComposeResult:
        yield MatchAwareScroll(id="preview_pane")


@pytest.mark.asyncio
async def test_the_thumb_survives_a_swap_and_then_describes_the_revealed_file() -> None:
    """Revealing changes ``opacity`` only, and ``preview/visibility.py`` keeps
    that free of layout on purpose — so a staged-row count taken during the
    arrange is never told what it counts has been revealed. On the swap's own
    ordering (hide the outgoing, then reveal) the stale count is the whole
    document, which subtracts the bar down to nothing: measured as the thumb
    disappearing at the hide and never coming back.
    """
    app = _EmptyPaneApp()
    async with app.run_test(size=(80, _HEIGHT)) as pilot:
        pane = app.query_one("#preview_pane", MatchAwareScroll)
        outgoing = await _preview(pane, "outgoing", 100)
        await pilot.pause()
        assert _thumb(pane)[1] > 0, "setup: no thumb before the swap even starts"

        incoming = await _preview(pane, "incoming", 300)
        set_preview_visibility(incoming, pre_reveal=True)
        await pilot.pause()
        assert pane.staged_rows == 300, (
            f"setup: the pane counted {pane.staged_rows} staged rows, so nothing is staged"
        )

        set_preview_visibility(outgoing, hidden=True)
        await pilot.pause()
        assert _thumb(pane)[1] > 0, (
            "the thumb vanished while the outgoing preview was hidden and the "
            "incoming one was still staged"
        )

        set_preview_visibility(incoming, pre_reveal=False)
        await pilot.pause()
        assert _thumb(pane)[1] > 0, "the thumb never came back after the reveal"
        assert pane.staged_rows == 0, (
            f"{pane.staged_rows} rows still counted as staged after the reveal"
        )


@pytest.mark.asyncio
async def test_a_scroll_into_the_staged_preview_does_not_pin_the_thumb() -> None:
    """The swap scrolls into the staged container before revealing it. Correcting
    the bar's extent while the reader's position sits past the corrected end
    leaves the ratio clamped at 1.0 — the thumb hard against the bottom of a
    track it is nowhere near the end of.
    """
    app = _EmptyPaneApp()
    async with app.run_test(size=(80, _HEIGHT)) as pilot:
        pane = app.query_one("#preview_pane", MatchAwareScroll)
        await _preview(pane, "outgoing", 100)
        staged = await _preview(pane, "incoming", 100)
        set_preview_visibility(staged, pre_reveal=True)
        await pilot.pause()
        pane.scroll_to(y=150, animate=False, immediate=True)
        await pilot.pause()
        assert pane.scroll_y == 150, (
            f"setup: the pane parked at {pane.scroll_y}, not in the staged half"
        )

        top, size = _thumb(pane)
        assert size > 0, "no thumb to place"
        assert top is not None
        assert top + size < _HEIGHT - 1, (
            f"thumb sits at {top}..{top + size} of {_HEIGHT} — pinned to the end of "
            f"the track while the reader is at row 150 of 200"
        )


@pytest.mark.asyncio
async def test_staging_does_not_give_a_short_preview_a_thumb() -> None:
    """A preview shorter than the viewport has nothing to scroll, so it has no
    thumb — and staging the next file behind it must not conjure one. The
    correction has to treat "shorter than the window" as fitting; a predicate
    that only asks whether the position is below the corrected end quietly
    excludes every such preview, which is the same gutter blink on short files.
    """
    app = _EmptyPaneApp()
    async with app.run_test(size=(80, _HEIGHT)) as pilot:
        pane = app.query_one("#preview_pane", MatchAwareScroll)
        await _preview(pane, "short", 10)
        await pilot.pause()
        alone = _thumb(pane)
        assert alone[1] == 0, f"setup: a 10-row preview should have no thumb, got {alone}"

        staged = await _preview(pane, "incoming", 300)
        set_preview_visibility(staged, pre_reveal=True)
        await pilot.pause()
        assert pane.staged_rows == 300, "setup: nothing was staged"

        assert _thumb(pane) == alone, (
            f"thumb went {alone} -> {_thumb(pane)} because a file was staged behind "
            f"a preview that does not fill the viewport"
        )


@pytest.mark.asyncio
async def test_a_reveal_reaches_the_bar_though_it_causes_no_layout() -> None:
    """Revealing changes ``opacity`` only, which ``preview/visibility.py`` keeps
    free of layout on purpose — so nothing re-runs ``arrange``. With the outgoing
    preview left on screen the reveal genuinely lengthens the document, and a
    staged-row count that waits for a layout keeps describing the shorter one.
    """
    app = _EmptyPaneApp()
    async with app.run_test(size=(80, _HEIGHT)) as pilot:
        pane = app.query_one("#preview_pane", MatchAwareScroll)
        await _preview(pane, "reading", 300)
        incoming = await _preview(pane, "incoming", 100)
        set_preview_visibility(incoming, pre_reveal=True)
        await pilot.pause()
        staged_thumb = _thumb(pane)
        assert staged_thumb[1] > 0, "setup: no thumb while staged"

        set_preview_visibility(incoming, pre_reveal=False)
        await pilot.pause()

        assert _thumb(pane)[1] < staged_thumb[1], (
            f"thumb stayed {staged_thumb} through a reveal that added 100 rows to "
            f"the document the reader can see (staged_rows={pane.staged_rows})"
        )
        assert pane.staged_rows == 0, (
            f"{pane.staged_rows} rows still counted as staged after the reveal"
        )
