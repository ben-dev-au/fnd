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

from pathlib import Path
from typing import cast

import pytest
from textual._animator import SimpleAnimation
from textual.app import App, ComposeResult
from textual.containers import Vertical
from textual.pilot import Pilot
from textual.widget import Widget
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


class _NestedApp(App[None]):
    """The topology a preview swap claims against: pane -> container -> chunk,
    with the match block one level further in."""

    CSS = """
    #container { height: auto; }
    #chunk { height: auto; }
    #block { height: 40; }
    """

    def compose(self) -> ComposeResult:
        with MatchAwareScroll(id="preview_pane"), Vertical(id="container"):
            yield Static("above", id="filler")
            with Vertical(id="chunk"):
                yield Static("match", id="block")


async def _nested(pilot: Pilot[None], app: _NestedApp):  # type: ignore[no-untyped-def]
    pane = app.query_one("#preview_pane", MatchAwareScroll)
    app.query_one("#filler", Static).styles.height = 500
    await pilot.pause()
    pane.scroll_y = 100
    await pilot.pause()
    return pane, app.query_one("#chunk", Vertical), app.query_one("#block", Static)


@pytest.mark.asyncio
async def test_a_chunk_claim_absorbs_a_prepend_in_its_container() -> None:
    """What the swap re-seats: a direct child of the container is the only depth
    a container-level prepend moves."""
    app = _NestedApp()
    async with app.run_test(size=(80, 24)) as pilot:
        pane, chunk, _block = await _nested(pilot, app)
        pane.absorb_anchor = (chunk, int(chunk.virtual_region.y))

        app.query_one("#filler", Static).styles.height = 560
        await pilot.pause()
        await pilot.pause()

        assert pane.scroll_y == 160, (
            f"scroll_y {pane.scroll_y} — a 60-row prepend above the claimed chunk "
            f"was not absorbed, so the reader was pushed down by it"
        )


@pytest.mark.asyncio
async def test_a_block_claim_cannot_see_that_prepend() -> None:
    """``virtual_region`` is measured against the immediate parent, so a claim on
    a block inside the chunk is inert — the reason the swap does not use one."""
    app = _NestedApp()
    async with app.run_test(size=(80, 24)) as pilot:
        pane, _chunk, block = await _nested(pilot, app)
        pane.absorb_anchor = (block, int(block.virtual_region.y))

        app.query_one("#filler", Static).styles.height = 560
        await pilot.pause()
        await pilot.pause()

        assert pane.scroll_y == 100, (
            f"scroll_y {pane.scroll_y} — a block-level claim moved the pane, so the "
            f"coordinate space this test pins has changed"
        )


@pytest.mark.asyncio
async def test_a_swap_leaves_the_claim_on_a_chunk_of_the_revealed_file(
    tmp_path: Path, tmp_index_dir: Path
) -> None:
    """After a cross-file reveal the claim must be a DIRECT child of the incoming
    container: the swap drops the old one (its scroll already accounts for the
    outgoing container leaving) and re-seats at a depth a prepend can move."""
    from fnd.index import build_index
    from fnd.tui import FNDApp
    from fnd.tui.widgets.preview_container import PreviewContainer
    from tests._pilot_wait import wait_until

    notes = tmp_path / "notes"
    notes.mkdir()
    body = "\n\n".join(f"Paragraph {i} mentioning templates." for i in range(60))
    for name in ("a.md", "b.md"):
        (notes / name).write_text(f"# {name}\n\n{body}\n", encoding="utf-8")
    build_index(roots=[notes], index_dir=tmp_index_dir, collection="notes")

    app = FNDApp(index_dir=tmp_index_dir, collection="notes", initial_query="templates")
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one("#preview_pane", MatchAwareScroll)
        await wait_until(
            pilot,
            lambda: app._preview.active is not None,
            timeout=15.0,
            message="no preview ever activated",
        )
        first = app._preview.active
        tree = app.query_one("#results_pane")
        tree.focus()
        await pilot.press("down")
        await wait_until(
            pilot,
            lambda: app._preview.active is not None and app._preview.active is not first,
            timeout=15.0,
            message="never navigated to the second file",
        )
        container = app._preview.active
        await wait_until(
            pilot,
            lambda: pane.absorb_anchor is not None,
            timeout=15.0,
            message="the swap never re-seated a claim",
        )
        claim = pane.absorb_anchor
        assert claim is not None
        claimed = cast(Widget, claim[0])
        assert isinstance(container, PreviewContainer)
        assert claimed.parent is container, (
            f"claim sits on {type(claimed).__name__} whose parent is "
            f"{type(claimed.parent).__name__} — only a direct child of the container "
            f"moves when a prepend lands, so this claim is inert"
        )


@pytest.mark.asyncio
async def test_a_claim_on_content_that_left_the_layout_is_dropped_not_absorbed() -> None:
    """``virtual_region`` reports ``Region()`` — y=0, no exception — for a widget
    whose ancestor is ``display: none``. Read as movement, that scrolls the pane
    backwards by the stored offset: measured 117-187 rows on a real swap."""
    app = _NestedApp()
    async with app.run_test(size=(80, 24)) as pilot:
        pane, chunk, _block = await _nested(pilot, app)
        pane.absorb_anchor = (chunk, int(chunk.virtual_region.y))
        claim = pane.absorb_anchor
        assert claim is not None
        assert claim[1] > 0, "the claim must sit at a non-zero offset to prove anything"
        before = pane.scroll_y

        # Only the claimed chunk leaves; the filler keeps the pane scrollable, so
        # any movement is the absorb's and not Textual clamping an empty pane.
        chunk.display = False
        await pilot.pause()
        await pilot.pause()

        assert pane.absorb_anchor is None, "a claim on content out of the layout was kept"
        assert pane.scroll_y == before, (
            f"scroll_y {pane.scroll_y} (was {before}) — the pane moved for content "
            f"that is no longer laid out"
        )


@pytest.mark.asyncio
async def test_no_frame_paints_the_document_at_the_uncorrected_offset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The absorb has to be in the arrangement the frame is painted from.

    Written from the ``virtual_size`` watcher it arrives one update cycle late:
    ``Screen._refresh_layout`` builds the map, calls ``_size_updated`` (where
    the watcher runs), then paints that same map. The scroll therefore reached
    the screen a cycle behind the prepend, so one frame showed the document a
    prepend-height out of place and the next snapped it back.
    """
    from textual._compositor import Compositor

    app = _PaneApp()
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        pane, filler, anchor = await _ready(pilot, app)
        _claim(pane, anchor)
        settled = anchor.region.y

        painted: list[int] = []
        render_update = Compositor.render_update

        def record(self: Compositor, *args: object, **kwargs: object) -> object:
            update = render_update(self, *args, **kwargs)  # type: ignore[arg-type]
            painted.append(anchor.region.y)
            return update

        monkeypatch.setattr(Compositor, "render_update", record)
        filler.styles.height = 560
        await pilot.pause()

        assert painted, "no frame was painted, so this proves nothing"
        assert set(painted) == {settled}, (
            f"the anchor painted at {sorted(set(painted))} — a 60-row prepend reached "
            f"the screen before the scroll absorbing it, so the document moved under "
            f"the reader for a frame (it sits at {settled})"
        )


@pytest.mark.asyncio
async def test_a_prepend_past_the_old_end_of_the_document_is_absorbed_in_full() -> None:
    """The absorb runs before the arrangement it belongs to has been applied, so
    ``validate_scroll_y`` would clamp it against a ``virtual_size`` that has not
    grown yet. Parked at the end of the document, that clamp is the whole
    correction: the reader is left where they were and the prepend shoves them
    backwards by its own height.
    """
    app = _PaneApp()
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        pane, filler, anchor = await _ready(pilot, app)
        filler.styles.height = 100
        await pilot.pause()
        pane.scroll_to(y=pane.max_scroll_y, animate=False, immediate=True)
        await pilot.pause()
        before = pane.scroll_y
        assert before > 0, "setup: the pane never scrolled"
        assert before == pane.max_scroll_y, (
            f"setup: parked at {before} of {pane.max_scroll_y}, not at the end"
        )

        _claim(pane, anchor)
        filler.styles.height = 200
        await pilot.pause()
        await pilot.pause()

        assert pane.scroll_y == before + 100, (
            f"scroll_y {pane.scroll_y}, wanted {before + 100} — a 100-row prepend was "
            f"clamped against the document size it was about to replace"
        )
