"""`● File types (every type)` was a state that could not exist.

Its summary and its compiled expression are identical to `○ no rule`, it saves
nothing, and it comes back as `○`, because the model collapses "every type
ticked" to no restriction, deliberately, so a file type added to the registry
tomorrow is not excluded by a box nobody could have ticked.

The tree simply went on showing the boxes ticked, so the screen held a state
the model had already discarded.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fnd.filters import FilterSpec
from fnd.filters.scan import SourceSample
from fnd.filters.tree_model import apply_selection, selection_for
from fnd.kinds import ALL_KIND_IDS
from fnd.tui import FNDApp
from fnd.tui.settings_screen import FilterBrowserScreen
from fnd.tui.widgets.toggle_tree import ToggleTree

_SAMPLE = SourceSample(kinds={"md": 2}, tags={"frontmatter": {"keep": 1}})


def test_the_model_collapses_it() -> None:
    """The premise, which is correct and stays."""
    every = {f"kind:{k}" for k in ALL_KIND_IDS}

    spec, _git, _fnd = apply_selection(FilterSpec(), every, set(), every)

    assert spec.kinds == ()


def test_and_the_tree_agrees_with_it() -> None:
    """The gap: what the spec means, read back as a selection."""
    every = {f"kind:{k}" for k in ALL_KIND_IDS}
    spec, _git, _fnd = apply_selection(FilterSpec(), every, set(), every)

    selected, _excluded = selection_for(spec)

    assert not {i for i in selected if i.startswith("kind:")}


@pytest.mark.asyncio
async def test_ticking_them_all_leaves_the_tree_showing_no_rule(tmp_index_dir: Path) -> None:
    app = FNDApp(index_dir=tmp_index_dir)
    async with app.run_test(size=(110, 34)) as pilot:
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
        screen = app.screen
        assert isinstance(screen, FilterBrowserScreen)
        tree = screen.query_one("#filter_tree", ToggleTree)

        # Toggled the way a user does (Enter on the branch ticks every child),
        # because posting the message by hand never touches the tree's own
        # selection, which is the half under test.
        tree.focus()
        tree.cursor_line = 0
        await pilot.pause()
        await pilot.press("enter")
        for _ in range(15):
            await pilot.pause()
        ticked = {i for i in tree.selected if i.startswith("kind:")}
        spec_kinds = screen._spec.kinds

    assert spec_kinds == (), spec_kinds
    assert not ticked, f"the tree still shows a state the model discarded: {sorted(ticked)[:3]}"
