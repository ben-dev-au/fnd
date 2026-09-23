"""Up at the top of a filters pane must settle, not bounce.

The tree sends Up to the bar when its cursor is on the top row (that is how the
row is keyboard-reachable); a bar that answers Up by focusing the tree again
makes focus oscillate forever between `✕ Clear N filters` and `File type`.

Reachable and escapable are not the same thing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fnd.filters import FilterSpec
from fnd.filters.scan import SourceSample
from fnd.tui import FNDApp
from fnd.tui.settings_screen import FilterBrowserScreen
from fnd.tui.widgets.clear_bar import ClearFiltersBar
from fnd.tui.widgets.toggle_tree import ToggleTree

_SAMPLE = SourceSample(kinds={"md": 3}, tags={"frontmatter": {"no_index": 1}})
_DEFAULTS = (FilterSpec(exclude_tags={"frontmatter": ("no_index",)}), True, True)


async def _browser_with_a_bar(app: FNDApp, pilot: object) -> FilterBrowserScreen:
    screen = FilterBrowserScreen(
        title="Index filters",
        spec=FilterSpec(kinds=("md",)),
        gitignore=True,
        fndignore=True,
        inherited=_DEFAULTS,
        sample_provider=lambda _spec: _SAMPLE,
        on_save=lambda *_a: None,
    )
    app.push_screen(screen)
    for _ in range(25):
        await pilot.pause()  # type: ignore[attr-defined]
    return screen


@pytest.mark.asyncio
async def test_repeated_up_settles_on_the_row(tmp_index_dir: Path) -> None:
    """Eight presses of Up must stop somewhere."""
    app = FNDApp(index_dir=tmp_index_dir)
    async with app.run_test(size=(110, 30)) as pilot:
        await pilot.pause()
        await _browser_with_a_bar(app, pilot)
        tree = app.screen.query_one("#filter_tree", ToggleTree)
        tree.focus()
        tree.cursor_line = 0
        for _ in range(4):
            await pilot.pause()
        seen: list[str] = []
        for _ in range(8):
            await pilot.press("up")
            for _ in range(3):
                await pilot.pause()
            focused = app.screen.focused
            seen.append(type(focused).__name__ if focused else "none")

    assert seen[-1] == ClearFiltersBar.__name__, seen
    assert seen[-4:] == [ClearFiltersBar.__name__] * 4, f"focus is bouncing: {seen}"


@pytest.mark.asyncio
async def test_down_from_the_row_still_reaches_the_tree(tmp_index_dir: Path) -> None:
    """The control: breaking the loop must not strand the row. Down is how
    you leave it, and it is the direction the tree actually lies in."""
    app = FNDApp(index_dir=tmp_index_dir)
    async with app.run_test(size=(110, 30)) as pilot:
        await pilot.pause()
        await _browser_with_a_bar(app, pilot)
        bar = app.screen.query_one("#clear_filters_bar", ClearFiltersBar)
        bar.focus()
        for _ in range(4):
            await pilot.pause()
        await pilot.press("down")
        for _ in range(4):
            await pilot.pause()
        focused = app.screen.focused

    assert isinstance(focused, ToggleTree), focused


@pytest.mark.asyncio
async def test_up_still_reaches_the_row_in_the_first_place(tmp_index_dir: Path) -> None:
    """The other control: it stays keyboard-reachable, which is what the Up
    handling is for."""
    app = FNDApp(index_dir=tmp_index_dir)
    async with app.run_test(size=(110, 30)) as pilot:
        await pilot.pause()
        await _browser_with_a_bar(app, pilot)
        tree = app.screen.query_one("#filter_tree", ToggleTree)
        tree.focus()
        tree.cursor_line = 0
        for _ in range(4):
            await pilot.pause()
        await pilot.press("up")
        for _ in range(4):
            await pilot.pause()
        focused = app.screen.focused

    assert isinstance(focused, ClearFiltersBar), focused
