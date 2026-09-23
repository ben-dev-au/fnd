"""The glyph legend says what the glyphs mean on the branch you are in.

One line claimed `●  index ONLY these` for the whole screen. On "Obey ignore
files" `●` means *obey this file*, which indexes FEWER files, and `○` means
more; the legend stated the opposite of what the row does.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fnd.filters import FilterSpec
from fnd.filters.scan import SourceSample
from fnd.filters.tree_model import (
    BOUND_LEGEND,
    IGNORE_LEGEND,
    KINDS_LEGEND,
    LEGEND,
    RULES_LEGEND,
    spec_branches,
)
from fnd.tui import FNDApp
from fnd.tui.settings_screen import FilterBrowserScreen
from fnd.tui.widgets.toggle_tree import ToggleTree

_SAMPLE = SourceSample(kinds={"md": 3}, tags={"frontmatter": {"no_index": 1}})


def test_only_the_branches_that_read_differently_override_it() -> None:
    by_id = {b.id: b for b in spec_branches(FilterSpec(), _SAMPLE)}

    assert by_id["ignore"].legend == IGNORE_LEGEND
    assert by_id["rules"].legend == RULES_LEGEND
    # `kinds` earned one: the shared line promises ⊘ and the model has no
    # exclude state for a file type.
    assert by_id["kinds"].legend == KINDS_LEGEND
    # These three are radio, so neither ⊘ nor ◐ can occur on them, and
    # "index ONLY these" is the wrong sentence for "Up to 1 MB".
    for name in ("size", "modified", "created"):
        assert by_id[name].legend == BOUND_LEGEND


def test_the_shared_line_no_longer_speaks_for_the_ignore_branch() -> None:
    """The control that names the defect: obeying an ignore file indexes
    fewer files, which is what `●` does there and the opposite of `index
    ONLY these`."""
    assert "index ONLY these" in LEGEND
    assert "index ONLY these" not in IGNORE_LEGEND
    assert "fewer" in IGNORE_LEGEND


@pytest.mark.asyncio
async def test_the_painted_legend_follows_the_cursor(tmp_index_dir: Path) -> None:
    app = FNDApp(index_dir=tmp_index_dir)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
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
            await pilot.pause()
        tree = app.screen.query_one("#filter_tree", ToggleTree)
        painted: dict[str, str] = {}
        for node in tree.root.children:
            tree.move_cursor(node)
            for _ in range(6):
                await pilot.pause()
            painted[str((node.data or {}).get("id"))] = "\n".join(
                "".join(s.text for s in strip) for strip in app.screen._compositor.render_strips()
            )

    assert "obey this file" in painted["ignore"], painted["ignore"].splitlines()[:3]
    assert "opens a branch" in painted["rules"], painted["rules"].splitlines()[:3]
    assert "index ONLY these" in painted["kinds"], painted["kinds"].splitlines()[:3]
    assert "obey this file" not in painted["kinds"], "the wording leaked between branches"


@pytest.mark.asyncio
async def test_enter_on_a_rules_branch_does_something(tmp_index_dir: Path) -> None:
    """It was the one row where Enter did nothing at all (no toggle, no
    expand) under a legend saying it opens an editor. Every branch beside it
    either toggles or expands, so this one expands, and the editor is then one
    row down where the legend says it is."""
    app = FNDApp(index_dir=tmp_index_dir)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
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
            await pilot.pause()
        tree = app.screen.query_one("#filter_tree", ToggleTree)
        node = next(n for n in tree.root.children if "Rules you type" in str(n.label))
        tree.move_cursor(node)
        for _ in range(4):
            await pilot.pause()
        before = node.is_expanded
        tree.action_toggle_selection()
        for _ in range(6):
            await pilot.pause()
        after = node.is_expanded

    assert not before, "the premise: it starts collapsed"
    assert after, "Enter did nothing on the one row whose legend promised the most"


class TestABranchKeepsItsName:
    """A row renamed itself as a side effect of an edit somewhere else.

    With one tag source showing rows, the branch took that SOURCE's label,
    so clearing a rule about the other source renamed `Tags` to
    `Note tags (YAML)` and took the `System tags` sub-branch with it. The
    user had changed a tag rule, not the shape of the pane.
    """

    _SAMPLE_ONE_SOURCE = SourceSample(kinds={"md": 3}, tags={"frontmatter": {"keep": 2}})

    def test_it_is_called_tags_with_two_sources(self) -> None:
        spec = FilterSpec(exclude_tags={"os": ("no_index",)})
        branch = next(
            b for b in spec_branches(spec, self._SAMPLE_ONE_SOURCE) if b.id.startswith("tags")
        )
        assert branch.label == "Tags"
        assert [g.label for g in branch.groups] == ["Note tags (YAML)", "System tags"]

    def test_and_still_called_tags_with_one(self) -> None:
        branch = next(
            b
            for b in spec_branches(FilterSpec(), self._SAMPLE_ONE_SOURCE)
            if b.id.startswith("tags")
        )
        assert branch.label == "Tags", "the branch renamed itself"

    def test_the_rows_are_still_collapsed_into_it(self) -> None:
        """The control: keeping the name must not reintroduce a pointless
        nesting level for a single source."""
        branch = next(
            b
            for b in spec_branches(FilterSpec(), self._SAMPLE_ONE_SOURCE)
            if b.id.startswith("tags")
        )
        assert not branch.groups, "a single source grew a sub-branch of its own"
        assert branch.items, "the single source's tags stopped being reachable"
