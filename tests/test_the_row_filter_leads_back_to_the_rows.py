"""Down from the filter tree's row filter leads back to the rows it narrowed.

`/` reaches the box and typing narrows the tree; without a Down bridge no key
leaves the box for what it narrowed. Every other settings screen bridges Down,
and this one is the longest list in the app.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fnd.filters import FilterSpec
from fnd.filters.scan import SourceSample
from fnd.tui import FNDApp
from fnd.tui.settings_screen import FilterBrowserScreen
from fnd.tui.widgets.toggle_tree import ToggleTree

_SAMPLE = SourceSample(kinds={"md": 3}, tags={"frontmatter": {"keep": 2, "draft": 1}})


async def _browser(app: FNDApp, pilot: object) -> FilterBrowserScreen:
    app.push_screen(
        FilterBrowserScreen(
            title="Index filters",
            spec=FilterSpec(),
            gitignore=True,
            fndignore=True,
            sample_provider=lambda _spec: _SAMPLE,
            on_save=lambda *_a: None,
        )
    )
    for _ in range(25):
        await pilot.pause()  # type: ignore[attr-defined]
    screen = app.screen
    assert isinstance(screen, FilterBrowserScreen)
    return screen


@pytest.mark.asyncio
async def test_down_from_the_filter_box_reaches_the_tree(tmp_index_dir: Path) -> None:
    app = FNDApp(index_dir=tmp_index_dir)
    async with app.run_test(size=(110, 34)) as pilot:
        await pilot.pause()
        await _browser(app, pilot)
        await pilot.press("slash")
        await pilot.pause()
        for ch in "tag":
            await pilot.press(ch)
        for _ in range(8):
            await pilot.pause()
        typing_in = type(app.focused).__name__

        await pilot.press("down")
        for _ in range(4):
            await pilot.pause()
        landed = type(app.focused).__name__

    assert typing_in == "Input", "the box must take the letters"
    assert landed == "ToggleTree", "and Down must leave it for the rows"


@pytest.mark.asyncio
async def test_the_tree_keeps_down_for_itself(tmp_index_dir: Path) -> None:
    """The control: the bridge must not steal Down from the tree."""
    app = FNDApp(index_dir=tmp_index_dir)
    async with app.run_test(size=(110, 34)) as pilot:
        await pilot.pause()
        screen = await _browser(app, pilot)
        tree = screen.query_one("#filter_tree", ToggleTree)
        tree.focus()
        await pilot.pause()
        before = tree.cursor_line

        await pilot.press("down")
        for _ in range(4):
            await pilot.pause()
        after = tree.cursor_line

    assert after == before + 1, (before, after)
