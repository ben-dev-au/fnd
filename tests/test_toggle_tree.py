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


NESTED = [
    ToggleGroup(
        "kinds",
        "File types",
        (),
        empty_label="every type",
        groups=(
            ToggleGroup(
                "docs", "Documents", (ToggleItem("pdf", "PDF"), ToggleItem("docx", "Word"))
            ),
            ToggleGroup("notes", "Notes", (ToggleItem("md", "Markdown"),)),
        ),
    ),
    ToggleGroup("tags", "Tags", (ToggleItem("a", "a"), ToggleItem("b", "b")), mode="cycle"),
]


class _Nested(App[None]):
    def compose(self) -> ComposeResult:
        yield ToggleTree(id="tt")

    def on_mount(self) -> None:
        self.query_one("#tt", ToggleTree).set_model(
            NESTED, set(), expanded={"kinds", "docs", "notes", "tags"}
        )


def _labels(tt: ToggleTree) -> dict[str, str]:
    out: dict[str, str] = {}
    stack = list(tt.root.children)
    while stack:
        node = stack.pop()
        data = node.data if isinstance(node.data, dict) else {}
        out[str(data.get("id"))] = str(node.label)
        stack.extend(node.children)
    return out


class TestNesting:
    @pytest.mark.asyncio
    async def test_a_group_of_groups_rolls_up_through_both_levels(self) -> None:
        app = _Nested()
        async with app.run_test() as pilot:
            tt = app.query_one("#tt", ToggleTree)
            tt.cursor_line = 2  # PDF
            await pilot.press("enter")
            await pilot.pause()
            labels = _labels(tt)
            assert labels["docs"].startswith("◐"), labels["docs"]
            assert labels["kinds"].startswith("◐"), labels["kinds"]

    @pytest.mark.asyncio
    async def test_toggling_the_top_group_cascades_to_every_descendant(self) -> None:
        app = _Nested()
        async with app.run_test() as pilot:
            tt = app.query_one("#tt", ToggleTree)
            tt.cursor_line = 0
            await pilot.press("enter")
            await pilot.pause()
            assert tt.selected == frozenset({"pdf", "docx", "md"})
            assert _labels(tt)["kinds"].startswith("●")

    @pytest.mark.asyncio
    async def test_an_empty_branch_says_what_that_means(self) -> None:
        app = _Nested()
        async with app.run_test() as pilot:
            await pilot.pause()
            assert "every type" in _labels(app.query_one("#tt", ToggleTree))["kinds"]

    @pytest.mark.asyncio
    async def test_one_excluded_tag_is_partial_not_a_blanket_exclusion(self) -> None:
        """⊘ on the branch said the whole category was excluded when one was."""
        app = _Nested()
        async with app.run_test() as pilot:
            tt = app.query_one("#tt", ToggleTree)
            tag_a = next(n for n in tt.root.children if str(n.label).endswith("Tags")).children[0]
            tt.cursor_line = tag_a.line
            await pilot.press("enter")  # include
            await pilot.press("enter")  # exclude
            await pilot.pause()
            assert tt.excluded == frozenset({"a"})
            assert _labels(tt)["tags"].startswith("◐"), _labels(tt)["tags"]


class TestNavigateOut:
    @pytest.mark.asyncio
    async def test_left_at_the_outermost_level_asks_the_host_to_leave(self) -> None:
        """Without this the binding swallows ← and the host's back never fires."""
        seen: list[bool] = []

        class _App(_Nested):
            @on(ToggleTree.NavigatedOut)
            def _out(self, _ev: ToggleTree.NavigatedOut) -> None:
                seen.append(True)

        app = _App()
        async with app.run_test() as pilot:
            tt = app.query_one("#tt", ToggleTree)
            tt.cursor_line = 2  # a leaf, two levels deep
            await pilot.press("left")  # -> parent group
            await pilot.pause()
            assert not seen, "a leaf must walk to its parent first"
            await pilot.press("left")  # collapse Documents
            await pilot.press("left")  # -> File types
            await pilot.press("left")  # collapse File types
            await pilot.pause()
            assert not seen
            await pilot.press("left")  # nothing left to collapse
            await pilot.pause()
            assert seen, "← at the top level did not ask the host to go back"
