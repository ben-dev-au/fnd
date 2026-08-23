"""Absorbing a prepend without painting the document at the wrong offset.

Mounting chunks ABOVE the viewport pushes everything below them down. Correcting
for that after a settled layout paints frames at the wrong offset first —
measured at 19 of 21 frames during a fill; correcting before it clamps against a
virtual size that has not grown and lands short. The pane compensates as the
layout lands instead.

Anchored on a widget rather than counting the height change: only content
ABOVE the anchor moves it, so a background fill appending BELOW is correctly
ignored, and a prepend that arrives in instalments — built chunks resolve their
heights over successive layouts — is absorbed in full. The claim is signed: a
prune removing chunks above the reader moves them by exactly the same rule.
"""

from __future__ import annotations

import pytest
from textual._animator import SimpleAnimation
from textual.app import App, ComposeResult
from textual.pilot import Pilot
from textual.widgets import Static

from fnd.tui.preview_scrollbar import MatchAwareScroll


class _PaneApp(App[None]):
    """A pane whose content height is driven directly, as a prepend drives it."""

    def compose(self) -> ComposeResult:
        with MatchAwareScroll(id="preview_pane"):
            yield Static("above", id="filler")
            yield Static("anchor", id="anchor")


async def _ready(pilot: Pilot[None], app: _PaneApp) -> tuple[MatchAwareScroll, Static, Static]:
    pane = app.query_one("#preview_pane", MatchAwareScroll)
    filler = app.query_one("#filler", Static)
    anchor = app.query_one("#anchor", Static)
    filler.styles.height = 500
    anchor.styles.height = 40
    await pilot.pause()
    pane.scroll_y = 100
    await pilot.pause()
    return pane, filler, anchor


def _claim(pane: MatchAwareScroll, anchor: Static) -> None:
    pane.absorb_anchor = (anchor, int(anchor.virtual_region.y))


@pytest.mark.asyncio
async def test_a_prepend_is_absorbed_as_the_layout_lands() -> None:
    app = _PaneApp()
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        pane, filler, anchor = await _ready(pilot, app)
        before = pane.scroll_y
        assert before == 100, f"setup: expected a scrolled pane, got {before}"

        _claim(pane, anchor)
        filler.styles.height = 560
        await pilot.pause()

        assert pane.scroll_y == before + 60, (
            f"the pane did not absorb the prepend: {before} -> {pane.scroll_y}"
        )


@pytest.mark.asyncio
async def test_a_prepend_arriving_in_instalments_is_absorbed_in_full() -> None:
    """Built chunks resolve their heights over successive layout passes."""
    app = _PaneApp()
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        pane, filler, anchor = await _ready(pilot, app)
        before = pane.scroll_y

        _claim(pane, anchor)
        filler.styles.height = 535
        await pilot.pause()
        filler.styles.height = 544
        await pilot.pause()

        assert pane.scroll_y == before + 44, (
            f"only part of the prepend was absorbed: {before} -> {pane.scroll_y}"
        )


@pytest.mark.asyncio
async def test_content_added_below_the_anchor_is_not_absorbed() -> None:
    """A background fill appends after the reader; nothing above them moved.

    A claim counting the pane's height change instead of the anchor's position
    would scroll them down by the appended height.
    """
    app = _PaneApp()
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        pane, _filler, anchor = await _ready(pilot, app)
        before = pane.scroll_y

        _claim(pane, anchor)
        anchor.styles.height = 400  # grows the document below the anchor
        await pilot.pause()

        assert pane.scroll_y == before, (
            f"an append below the reader was absorbed as a prepend: {before} -> {pane.scroll_y}"
        )


@pytest.mark.asyncio
async def test_content_removed_above_the_anchor_is_absorbed() -> None:
    """What a prune takes off the top, the reader is scrolled back by.

    Compensating in the prune itself cannot work: Textual defers ``remove()``,
    so the scroll is applied against a virtual size that still counts the
    removed chunks.
    """
    app = _PaneApp()
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        pane, filler, anchor = await _ready(pilot, app)
        before = pane.scroll_y

        _claim(pane, anchor)
        filler.styles.height = 460
        await pilot.pause()

        assert pane.scroll_y == before - 40, (
            f"the reader did not follow the removal: {before} -> {pane.scroll_y}"
        )


@pytest.mark.asyncio
async def test_an_in_flight_scroll_animation_is_retargeted_not_stopped() -> None:
    """Force-stopping COMPLETES an animation, teleporting the reader to its end.

    The animation's endpoints are in pre-prepend coordinates, so absorbing
    without moving them lets it drive scroll_y back and undo the absorb.
    """
    app = _PaneApp()
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        pane, filler, anchor = await _ready(pilot, app)

        pane.scroll_to(y=300, animate=True, duration=5.0)
        await pilot.pause()
        key = (id(pane), "scroll_y")
        animation = app.animator._animations.get(key)
        assert isinstance(animation, SimpleAnimation), "setup: no animation in flight"
        end_before = animation.end_value
        assert isinstance(end_before, (int, float)), "setup: a scroll animation ends on a number"

        _claim(pane, anchor)
        filler.styles.height = 560
        await pilot.pause()

        assert app.animator._animations.get(key) is animation, (
            "the animation was dropped; force-stopping completes it and teleports the reader"
        )
        assert animation.end_value == end_before + 60, (
            f"the destination was not moved with the document: "
            f"{end_before} -> {animation.end_value}"
        )
