"""Behavioural tests for the reusable ToggleTree — the three bug classes the
ad-hoc trees hit must be impossible here."""

from __future__ import annotations

import pytest
from textual import on
from textual.app import App, ComposeResult

from fnd.tui.widgets.toggle_tree import ToggleGroup, ToggleItem, ToggleTree

GROUPS = [
    ToggleGroup("code", "Code", (ToggleItem("py", "Python"), ToggleItem("cpp", "C++"))),
    ToggleGroup("data", "Data", (ToggleItem("json", "JSON"),)),
]
# Rendered line order (root hidden, both groups expanded):
#  0 Code (group)   1 Python   2 C++   3 Data (group)   4 JSON


class _Harness(App[None]):
    def __init__(self) -> None:
        super().__init__()
        self.changes: list[set[str]] = []

    def compose(self) -> ComposeResult:
        yield ToggleTree(id="tt")

    def on_mount(self) -> None:
        tt = self.query_one("#tt", ToggleTree)
        tt.set_model(GROUPS, set(), expanded={"code", "data"})
        tt.focus()

    @on(ToggleTree.SelectionChanged)
    def _record(self, ev: ToggleTree.SelectionChanged) -> None:
        self.changes.append(set(ev.selected))


@pytest.mark.asyncio
async def test_enter_toggles_item_without_moving_cursor() -> None:
    app = _Harness()
    async with app.run_test() as pilot:
        await pilot.pause()
        tt = app.query_one("#tt", ToggleTree)
        tt.cursor_line = 1  # Python
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert tt.selected == frozenset({"py"})
        assert tt.cursor_line == 1, "cursor must not jump after a toggle"
        assert app.changes[-1] == {"py"}
        # Toggle off again.
        await pilot.press("enter")
        await pilot.pause()
        assert tt.selected == frozenset()


@pytest.mark.asyncio
async def test_enter_on_group_toggles_all_members() -> None:
    app = _Harness()
    async with app.run_test() as pilot:
        await pilot.pause()
        tt = app.query_one("#tt", ToggleTree)
        tt.cursor_line = 0  # Code group
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert tt.selected == frozenset({"py", "cpp"})
        # And the group node stays expanded (Enter must NOT collapse it).
        assert tt.root.children[0].is_expanded
        await pilot.press("enter")
        await pilot.pause()
        assert tt.selected == frozenset()


@pytest.mark.asyncio
async def test_enter_on_collapsed_group_toggles_not_expands() -> None:
    app = _Harness()
    async with app.run_test() as pilot:
        await pilot.pause()
        tt = app.query_one("#tt", ToggleTree)
        tt.root.children[0].collapse()
        await pilot.pause()
        tt.cursor_line = 0
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert tt.selected == frozenset({"py", "cpp"})
        assert not tt.root.children[0].is_expanded, "Enter must not expand the group"


@pytest.mark.asyncio
async def test_arrows_expand_and_collapse() -> None:
    app = _Harness()
    async with app.run_test() as pilot:
        await pilot.pause()
        tt = app.query_one("#tt", ToggleTree)
        tt.root.children[0].collapse()
        await pilot.pause()
        tt.cursor_line = 0
        await pilot.pause()
        await pilot.press("right")
        await pilot.pause()
        assert tt.root.children[0].is_expanded
        assert tt.selected == frozenset(), "expand must not toggle"
        await pilot.press("left")
        await pilot.pause()
        assert not tt.root.children[0].is_expanded
        assert tt.selected == frozenset(), "collapse must not toggle"


@pytest.mark.asyncio
async def test_click_toggles_the_clicked_row() -> None:
    app = _Harness()
    async with app.run_test() as pilot:
        await pilot.pause()
        tt = app.query_one("#tt", ToggleTree)
        # Click the C++ leaf (line 2). Offset into the tree region.
        await pilot.click("#tt", offset=(4, 2))
        await pilot.pause()
        assert "cpp" in tt.selected, f"click should toggle the clicked row: {tt.selected}"
        assert app.changes, "click must emit SelectionChanged"


@pytest.mark.asyncio
async def test_group_marker_tri_state() -> None:
    app = _Harness()
    async with app.run_test() as pilot:
        await pilot.pause()
        tt = app.query_one("#tt", ToggleTree)
        code = tt.root.children[0]
        assert "○" in str(code.label)  # none selected
        tt.cursor_line = 1
        await pilot.press("enter")  # select Python only
        await pilot.pause()
        assert "◐" in str(code.label), f"partial expected: {code.label!r}"
        tt.cursor_line = 2
        await pilot.press("enter")  # select C++ too
        await pilot.pause()
        assert "●" in str(code.label), f"full expected: {code.label!r}"
