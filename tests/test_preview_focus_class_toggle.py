"""Preview pane focus-indicator border is driven by a plain class
(``-focused``) instead of CSS ``:focus-within``.

Why this matters: a ``:focus-within`` rule on the preview pane marks
every descendant as focus-sensitive in Textual's stylesheet, so every
focus change forces a full subtree CSS reapply across the pane's chunk
widgets / MarkdownBlocks / flat-buffer rows. Toggling a plain class
keeps the visual feedback while letting Textual skip the subtree work.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from textual.widgets import Tree

from fnd.index import build_index
from fnd.tui import FNDApp


@pytest.fixture
def built_index(fixtures_dir: Path, tmp_index_dir: Path) -> Path:
    build_index(roots=[fixtures_dir], index_dir=tmp_index_dir, collection="default")
    return tmp_index_dir


@pytest.mark.asyncio
async def test_preview_pane_has_no_focus_within_rule(built_index: Path) -> None:
    """The pane's applicable styles must NOT include any ``:focus-within``
    pseudo-class — otherwise Textual reapplies CSS across the entire
    preview subtree on every focus change.
    """
    app = FNDApp(index_dir=built_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        pane = app.query_one("#preview_pane")
        assert pane._has_focus_within is False, (
            "preview_pane has ':focus-within' in its applicable CSS, which "
            "would trigger a full-subtree restyle on every focus change"
        )


@pytest.mark.asyncio
async def test_preview_focused_class_toggles_with_focus(built_index: Path) -> None:
    """The ``-focused`` class lands on the preview pane when (and only
    when) focus is inside it."""
    app = FNDApp(index_dir=built_index, initial_query="blue penguin sandwich")
    async with app.run_test() as pilot:
        await pilot.pause()
        pane = app.query_one("#preview_pane")
        # Focus elsewhere — class should be absent.
        app.query_one("#results_pane", Tree).focus()
        await pilot.pause()
        assert "-focused" not in pane.classes

        # Focus the preview pane — class should appear.
        pane.focus()
        await pilot.pause()
        assert "-focused" in pane.classes

        # Move focus back out — class should clear.
        app.query_one("#results_pane", Tree).focus()
        await pilot.pause()
        assert "-focused" not in pane.classes


@pytest.mark.asyncio
async def test_focus_border_class_moves_between_panes(built_index: Path) -> None:
    """The accent border (``-focused``) follows focus across every pane —
    not just the preview — and only the focused pane wears it."""
    app = FNDApp(index_dir=built_index, initial_query="blue penguin sandwich")
    async with app.run_test() as pilot:
        await pilot.pause()
        results = app.query_one("#results_pane", Tree)
        preview = app.query_one("#preview_pane")

        results.focus()
        await pilot.pause()
        assert "-focused" in results.classes
        assert "-focused" not in preview.classes

        preview.focus()
        await pilot.pause()
        assert "-focused" in preview.classes
        assert "-focused" not in results.classes


@pytest.mark.asyncio
async def test_focus_border_survives_terminal_blur(built_index: Path) -> None:
    """The accent border must NOT drop when the terminal loses focus.

    Textual clears widget focus on ``AppBlur`` (``app_focus = False`` →
    ``screen.set_focus(None)``) and only restores it on the next keypress —
    which made the border vanish on tab-away and reappear a beat late. The
    persistent ``-focused`` class is driven by descendant *focus*, never
    cleared on blur, so it (and the border) stays put across the blur/refocus
    cycle."""
    app = FNDApp(index_dir=built_index, initial_query="blue penguin sandwich")
    async with app.run_test() as pilot:
        await pilot.pause()
        results = app.query_one("#results_pane", Tree)
        results.focus()
        await pilot.pause()
        assert "-focused" in results.classes

        # Terminal loses focus: Textual blurs the focused widget.
        app.app_focus = False
        await pilot.pause()
        assert app.screen.focused is None, "precondition: blur cleared widget focus"
        assert "-focused" in results.classes, "border dropped on terminal blur"

        # Terminal regains focus: border was never lost, still present.
        app.app_focus = True
        await pilot.pause()
        assert "-focused" in results.classes


@pytest.mark.asyncio
async def test_focus_change_does_not_walk_preview_subtree(built_index: Path) -> None:
    """The pane's ``watch_has_focus`` override must not walk the subtree.

    The stock ``Widget.watch_has_focus`` calls ``update_node_styles``,
    which walks every descendant and reapplies CSS — the dominant cost
    of focus transitions on large documents. The override on
    ``MatchAwareScroll`` applies CSS only to the pane itself.
    """
    app = FNDApp(index_dir=built_index, initial_query="results")
    async with app.run_test() as pilot:
        await pilot.pause()
        # Drill into a result to populate the preview subtree.
        results = app.query_one("#results_pane", Tree)
        results.focus()
        await pilot.pause()
        for _ in range(5):
            await pilot.press("down")
            await pilot.pause()
        for _ in range(5):
            await pilot.pause()

        pane = app.query_one("#preview_pane")
        original_walk = pane.walk_children
        walk_count = [0]

        def patched_walk(*a: Any, **kw: Any) -> Any:
            walk_count[0] += 1
            return original_walk(*a, **kw)

        pane.walk_children = patched_walk  # type: ignore[method-assign]

        # Toggle focus in and out; the pane must not walk its own subtree.
        pane.focus()
        await pilot.pause()
        results.focus()
        await pilot.pause()

        assert walk_count[0] == 0, (
            f"focus changes walked the preview subtree {walk_count[0]} time(s); "
            "the watch_has_focus override should keep work bounded to the pane"
        )
