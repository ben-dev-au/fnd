"""A clause typed in the text form and not owned by a picker has a row.

Decomposition puts what the pickers cannot render into ``spec.raw``. A tree
naming ``spec.expression`` only shows the first of two appended clauses, which
reads as a row two edits out of date.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fnd.filters import FilterSpec
from fnd.filters.scan import SourceSample
from fnd.filters.tree_model import spec_branches

_SAMPLE = SourceSample(kinds={"md": 2}, tags={"frontmatter": {"keep": 1}})


def _rows(spec: FilterSpec) -> str:
    return " || ".join(
        f"{b.label} :: {' | '.join(item[1] for item in b.items)}"
        for b in spec_branches(spec, _SAMPLE)
    )


def test_a_raw_clause_is_named_somewhere() -> None:
    spec = FilterSpec(
        expression="file.size < 500000",
        raw=("NOT (file.name ~~ 'scratch-*')", "file.size > 10"),
    )
    rows = _rows(spec)

    assert "scratch-*" in rows, rows
    assert "file.size > 10" in rows, rows


def test_the_count_includes_them() -> None:
    """`(1 set)` over three typed clauses is the same claim in a label."""
    spec = FilterSpec(
        expression="file.size < 500000",
        raw=("NOT (file.name ~~ 'scratch-*')", "file.size > 10"),
    )
    counts = [b.label for b in spec_branches(spec, _SAMPLE) if "(" in b.label]

    assert any("3" in lbl for lbl in counts), counts


def test_enter_on_one_reaches_the_editor_that_owns_it() -> None:
    """No per-field editor can hold a raw clause, so its row hands over."""
    spec = FilterSpec(raw=("file.size > 10",))
    ids = [
        item[0]
        for b in spec_branches(spec, _SAMPLE)
        for item in b.items
        if "file.size > 10" in item[1]
    ]

    assert ids, "no row for it"
    assert all(i.startswith("rule:raw:") for i in ids), ids


def test_a_spec_with_no_raw_clause_says_nothing_extra() -> None:
    """The control: the branch must not appear for a set that has none."""
    rows = _rows(FilterSpec(expression="file.size < 500000"))

    assert "Set in the text form" not in rows, rows


@pytest.mark.asyncio
async def test_enter_on_a_typed_rule_opens_the_text_form() -> None:
    """The routing, driven rather than asserted on an id prefix.

    `_on_action`'s prefix tuple must admit `rule:raw:`; checking the row's id
    alone passes while Enter on a Typed rule is a dead key.
    """
    from fnd.tui import FNDApp
    from fnd.tui.settings_screen import FilterBrowserScreen, FilterTextScreen
    from fnd.tui.widgets.toggle_tree import ToggleTree

    app = FNDApp(index_dir=Path("/nonexistent-index"))
    async with app.run_test(size=(110, 34)) as pilot:
        await pilot.pause()
        screen = FilterBrowserScreen(
            title="Index filters",
            spec=FilterSpec(expression="file.size < 500000", raw=("file.size > 10",)),
            gitignore=True,
            fndignore=True,
            sample_provider=lambda _spec: _SAMPLE,
            on_save=lambda *_a: None,
        )
        app.push_screen(screen)
        for _ in range(25):
            await pilot.pause()
        tree = screen.query_one("#filter_tree", ToggleTree)

        # Pressing the key, not posting the message: `ActionSelected` only
        # fires when the group's mode is "actions", so a hand-posted message
        # passes even if the branch stops being one.
        def _line_for(row_id: str) -> int:
            for line in range(len(tree._tree_lines)):
                node = tree.get_node_at_line(line)
                data = getattr(node, "data", None) or {}
                if isinstance(data, dict) and data.get("id") == row_id:
                    return line
            raise AssertionError(f"{row_id!r} is not on the tree")

        tree.focus()
        tree.cursor_line = _line_for("rules")
        await pilot.pause()
        await pilot.press("right")
        for _ in range(5):
            await pilot.pause()
        tree.cursor_line = _line_for("rule:raw:0")
        await pilot.pause()
        await pilot.press("enter")
        for _ in range(10):
            await pilot.pause()
        landed = app.screen

    assert isinstance(landed, FilterTextScreen), type(landed).__name__
