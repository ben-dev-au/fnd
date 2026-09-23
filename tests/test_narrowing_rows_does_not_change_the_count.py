"""A row filter hides rows; it does not shrink what "all of them" means.

Typing `python` narrowed File types to one row, and ticking it painted
`● File types  (every type)` while the expression underneath read
`['python']`. The screen contradicted itself on the same frame.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fnd.filters import FilterSpec
from fnd.filters.scan import SourceSample
from fnd.tui import FNDApp
from fnd.tui.settings_screen import FilterBrowserScreen
from fnd.tui.widgets.toggle_tree import ToggleGroup, ToggleItem

_SAMPLE = SourceSample(kinds={"md": 3, "python": 1}, tags={"frontmatter": {"keep": 2}})


def test_a_narrowed_group_still_counts_what_it_holds() -> None:
    from fnd.tui.settings_screen import _matching_group

    group = ToggleGroup(
        "kinds",
        "File types",
        (),
        groups=(
            ToggleGroup("kinds:notes", "Notes & text", (ToggleItem("kind:md", "Markdown"),)),
            ToggleGroup("kinds:code", "Code", (ToggleItem("kind:py", "Python"),)),
        ),
    )
    narrowed = _matching_group(group, "python")

    assert narrowed is not None
    assert len(narrowed.leaves) == 1, "the premise: one row survives"
    assert len(narrowed.counted_leaves) == 2, "the branch still holds two"


def test_nothing_is_counted_twice() -> None:
    """The control: an inflated denominator is the same defect as a shrunken
    one, and a nested group can contribute its leaves at two levels."""
    from fnd.tui.settings_screen import _matching_group

    group = ToggleGroup(
        "kinds",
        "File types",
        (ToggleItem("kind:md", "Markdown"),),
        groups=(
            ToggleGroup(
                "kinds:code",
                "Code",
                (ToggleItem("kind:py", "Python"), ToggleItem("kind:rs", "Rust")),
            ),
        ),
    )
    narrowed = _matching_group(group, "python")

    assert narrowed is not None
    ids = [i.id for i in narrowed.counted_leaves]
    assert sorted(ids) == ["kind:md", "kind:py", "kind:rs"], ids


@pytest.mark.asyncio
async def test_the_screen_does_not_contradict_itself(tmp_index_dir: Path) -> None:
    app = FNDApp(index_dir=tmp_index_dir)
    async with app.run_test(size=(120, 34)) as pilot:
        await pilot.pause()
        screen = FilterBrowserScreen(
            title="Index filters",
            spec=FilterSpec(kinds=("python",)),
            gitignore=True,
            fndignore=True,
            sample_provider=lambda _spec: _SAMPLE,
            on_save=lambda *_a: None,
        )
        app.push_screen(screen)
        for _ in range(25):
            await pilot.pause()
        screen._query = "python"
        screen._rebuild(focus_tree=False)
        for _ in range(8):
            await pilot.pause()
        on_screen = "\n".join(
            "".join(s.text for s in strip) for strip in app.screen._compositor.render_strips()
        )

    branch = next(line for line in on_screen.splitlines() if "File types" in line)
    assert "every type" not in branch, branch
    assert "◐" in branch, branch
