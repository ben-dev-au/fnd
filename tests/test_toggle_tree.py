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
            await pilot.press("enter")  # exclude, the first state
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


def test_the_first_press_excludes_rather_than_narrowing_to_one_tag() -> None:
    """Include here means "index only files carrying this", so one press on a
    tag could take a collection from eight files to one and need a reindex to
    undo. Exclude is the commoner intent and the recoverable one."""
    from fnd.tui.widgets.toggle_tree import ToggleTree

    tree = ToggleTree()
    tree._selected, tree._excluded = set(), set()
    tree._cycle("tag:frontmatter:draft")
    assert tree._excluded == {"tag:frontmatter:draft"}
    assert not tree._selected, "the first press must not write include_tags"

    tree._cycle("tag:frontmatter:draft")
    assert tree._selected == {"tag:frontmatter:draft"}
    tree._cycle("tag:frontmatter:draft")
    assert not tree._selected, "the third press clears include"
    assert not tree._excluded, "the third press clears exclude"


def test_the_legend_says_what_each_state_does_to_the_index() -> None:
    """`keep only these` reads as a preference; it removes everything else."""
    from fnd.filters.tree_model import LEGEND

    assert "never index" in LEGEND
    assert "ONLY" in LEGEND


@pytest.mark.asyncio
async def test_enter_on_a_tag_branch_does_not_discard_the_exclusions() -> None:
    """It wiped every selection and exclusion on the branch, with no confirm
    and no undo, while the same key on a file-type branch means "select all"."""
    app = _Nested()
    async with app.run_test() as pilot:
        tt = app.query_one("#tt", ToggleTree)
        tags = next(n for n in tt.root.children if str(n.label).endswith("Tags"))
        tt.cursor_line = tags.children[0].line
        await pilot.press("enter")  # exclude the first tag
        await pilot.pause()
        assert tt.excluded, "precondition: something to lose"
        before = set(tt.excluded)

        tt.cursor_line = tags.line
        await pilot.press("enter")
        await pilot.pause()
        assert set(tt.excluded) == before, "the branch row discarded the exclusions"


class TestABranchSaysHowMuchIsOn:
    """A marker says only *that* a branch is partly on, so a collapsed one hid
    how much, while the radio branches beside it named their choice.

    Few enough to name are named: `1 of 2 types` describes the setting, `PDF`
    describes what it does. Past three it counts again.
    """

    class _Counted(App[None]):
        def compose(self) -> ComposeResult:
            yield ToggleTree(id="tt")

        def on_mount(self) -> None:
            groups = [
                ToggleGroup(
                    "kinds",
                    "File types",
                    (ToggleItem("pdf", "PDF"), ToggleItem("md", "Markdown")),
                    noun="types",
                ),
                ToggleGroup(
                    "tags",
                    "Tags",
                    (ToggleItem("a", "a"), ToggleItem("b", "b")),
                    mode="cycle",
                    noun="tags",
                ),
            ]
            self.query_one("#tt", ToggleTree).set_model(
                groups, {"pdf"}, excluded={"a"}, expanded=set()
            )

    @pytest.mark.asyncio
    async def test_a_partly_ticked_branch_names_what_is_on(self) -> None:
        app = self._Counted()
        async with app.run_test() as pilot:
            await pilot.pause()
            assert "PDF" in _labels(app.query_one("#tt", ToggleTree))["kinds"]

    @pytest.mark.asyncio
    async def test_an_excluding_branch_names_the_exclusions(self) -> None:
        app = self._Counted()
        async with app.run_test() as pilot:
            await pilot.pause()
            assert "a excluded" in _labels(app.query_one("#tt", ToggleTree))["tags"]

    @pytest.mark.asyncio
    async def test_an_all_ticked_branch_does_not_repeat_its_marker(self) -> None:
        app = self._Counted()
        async with app.run_test() as pilot:
            tt = app.query_one("#tt", ToggleTree)
            tt.set_model(
                [
                    ToggleGroup(
                        "kinds",
                        "File types",
                        (ToggleItem("pdf", "PDF"), ToggleItem("md", "Markdown")),
                        noun="types",
                    )
                ],
                {"pdf", "md"},
                expanded=set(),
            )
            await pilot.pause()
            label = _labels(tt)["kinds"]
            assert label.startswith("●")
            assert "of 2 types" not in label, label


class TestARollupSurvivesACategoryToggle:
    """`_repaint_group` repaints a node and everything BELOW it. Only the item
    path repainted ancestors, so toggling a category left its parent reading
    its old count for the rest of the session."""

    class _TwoDeep(App[None]):
        def compose(self) -> ComposeResult:
            yield ToggleTree(id="tt")

        def on_mount(self) -> None:
            groups = [
                ToggleGroup(
                    "kinds",
                    "File types",
                    (),
                    noun="types",
                    groups=(
                        ToggleGroup("docs", "Documents", (ToggleItem("pdf", "PDF"),)),
                        ToggleGroup("notes", "Notes", (ToggleItem("md", "Markdown"),)),
                    ),
                )
            ]
            self.query_one("#tt", ToggleTree).set_model(
                groups, {"pdf"}, expanded={"kinds", "docs", "notes"}
            )

    @pytest.mark.asyncio
    async def test_toggling_a_category_updates_the_branch_above_it(self) -> None:
        app = self._TwoDeep()
        async with app.run_test() as pilot:
            tt = app.query_one("#tt", ToggleTree)
            await pilot.pause()
            assert "PDF" in _labels(tt)["kinds"]
            notes = next(
                n
                for n in tt.root.children[0].children
                if isinstance(n.data, dict) and n.data.get("id") == "notes"
            )
            tt.cursor_line = notes.line
            await pilot.press("enter")
            await pilot.pause()
            assert tt.selected == frozenset({"pdf", "md"})
            assert "of 2 types" not in _labels(tt)["kinds"], _labels(tt)["kinds"]


@pytest.mark.asyncio
async def test_one_tag_under_two_sources_counts_once() -> None:
    """A tag is drawn under every source that can carry it, so counting rows
    reported a single excluded `no_index` as two."""

    class _Tags(App[None]):
        def compose(self) -> ComposeResult:
            yield ToggleTree(id="tt")

        def on_mount(self) -> None:
            self.query_one("#tt", ToggleTree).set_model(
                [
                    ToggleGroup(
                        "tags",
                        "Tags",
                        (),
                        mode="cycle",
                        noun="tags",
                        groups=(
                            ToggleGroup(
                                "tags:os", "System", (ToggleItem("tag:os:no_index", "no_index"),)
                            ),
                            ToggleGroup(
                                "tags:fm",
                                "Note",
                                (ToggleItem("tag:frontmatter:no_index", "no_index"),),
                            ),
                        ),
                    )
                ],
                set(),
                excluded={"tag:os:no_index", "tag:frontmatter:no_index"},
                expanded=set(),
            )

    app = _Tags()
    async with app.run_test() as pilot:
        await pilot.pause()
        label = _labels(app.query_one("#tt", ToggleTree))["tags"]
        assert "no_index excluded" in label, label
        assert label.count("no_index") == 1, f"named once per tag, not per source: {label}"


@pytest.mark.asyncio
async def test_a_tree_opens_with_a_cursor() -> None:
    """It opened with `cursor_line = -1`, so the first ⏎ or ↓ was spent making
    a cursor and looked like a dead key."""
    app = _Nested()
    async with app.run_test() as pilot:
        await pilot.pause()
        tree = app.query_one("#tt", ToggleTree)
        assert tree.cursor_line == 0
        assert tree.cursor_node is not None


@pytest.mark.asyncio
async def test_both_ends_of_a_no_restriction_branch_read_alike() -> None:
    """Ticking every file type is the same no-restriction as ticking none
    (`kinds` collapses to () either way), but one painted `○ (every type)` and
    the other a bare `●`, which the legend reads as "index ONLY these"."""

    class _Kinds(App[None]):
        def compose(self) -> ComposeResult:
            yield ToggleTree(id="tt")

        def on_mount(self) -> None:
            self.query_one("#tt", ToggleTree).set_model(
                [
                    ToggleGroup(
                        "kinds",
                        "File types",
                        (ToggleItem("md", "Markdown"), ToggleItem("pdf", "PDF")),
                        empty_label="every type",
                        full_label="every type",
                        noun="types",
                    )
                ],
                set(),
                expanded=set(),
            )

    app = _Kinds()
    async with app.run_test() as pilot:
        tree = app.query_one("#tt", ToggleTree)
        await pilot.pause()
        assert "every type" in _labels(tree)["kinds"]
        tree.set_model(
            [
                ToggleGroup(
                    "kinds",
                    "File types",
                    (ToggleItem("md", "Markdown"), ToggleItem("pdf", "PDF")),
                    empty_label="every type",
                    full_label="every type",
                    noun="types",
                )
            ],
            {"md", "pdf"},
            expanded=set(),
        )
        await pilot.pause()
        label = _labels(tree)["kinds"]
        assert label.startswith("●")
        assert "every type" in label, label


@pytest.mark.asyncio
async def test_a_branch_without_a_full_label_is_unchanged() -> None:
    """`Obey ignore files` with both on genuinely means both, not "no rule"."""
    app = _Nested()
    async with app.run_test() as pilot:
        tree = app.query_one("#tt", ToggleTree)
        await pilot.pause()
        tree.set_model(
            [ToggleGroup("pair", "Pair", (ToggleItem("a", "A"), ToggleItem("b", "B")))],
            {"a", "b"},
            expanded=set(),
        )
        await pilot.pause()
        assert _labels(tree)["pair"] == "●  Pair"


class TestCountingWhatTheUserCanTellApart:
    """The dedup keyed on the label, but a tag row's label carries its file
    count, so the same tag seen 1 and 2 times looked like two tags. And the
    noun never singularised: "only 1 tags".

    The same dedup has to hold when the summary NAMES what is on rather than
    counting it, or one tag under two sources reads "x, x".
    """

    @staticmethod
    def _tree(counts: tuple[int, int]) -> ToggleTree:
        tree = ToggleTree("F")
        group = ToggleGroup(
            "tags",
            "Tags",
            (),
            mode="cycle",
            noun="tags",
            groups=(
                ToggleGroup(
                    "tags:os",
                    "System",
                    (ToggleItem("tag:os:x", f"x  ({counts[0]})", "x"),),
                ),
                ToggleGroup(
                    "tags:fm",
                    "Note",
                    (ToggleItem("tag:frontmatter:x", f"x  ({counts[1]})", "x"),),
                ),
            ),
        )
        tree._by_id = {g.id: g for g in group.walk()}
        tree._selected = set()
        tree._excluded = {"tag:os:x", "tag:frontmatter:x"}
        return tree

    def test_one_tag_counts_once_whatever_its_counts(self) -> None:
        for counts in ((2, 2), (1, 2), (5, 1)):
            tree = self._tree(counts)
            label = str(tree._group_label(tree._by_id["tags"]))
            assert "x excluded" in label, f"counts {counts} gave {label}"
            assert label.count("x excluded") == 1, f"counts {counts} gave {label}"

    def test_the_noun_agrees_with_the_number(self) -> None:
        """Past the naming threshold it counts again, and the noun must agree."""
        tree = ToggleTree("F")
        group = ToggleGroup(
            "tags",
            "Tags",
            tuple(ToggleItem(f"tag:fm:{n}", n, n) for n in ("a", "b", "c", "d")),
            mode="cycle",
            noun="tags",
        )
        tree._by_id = {g.id: g for g in group.walk()}
        tree._selected = {"tag:fm:a", "tag:fm:b", "tag:fm:c", "tag:fm:d"}
        tree._excluded = set()

        label = str(tree._group_label(tree._by_id["tags"]))

        assert "only 4 tags" in label, label

    def test_one_on_reads_as_one_thing(self) -> None:
        """And below it, the singular case names the tag rather than counting."""
        tree = self._tree((1, 2))
        tree._selected = {"tag:os:x", "tag:frontmatter:x"}
        tree._excluded = set()

        label = str(tree._group_label(tree._by_id["tags"]))

        assert "only x" in label, label
        assert "1 tags" not in label, label
