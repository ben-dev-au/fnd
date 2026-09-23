"""The gestures that leave a settings editor work on every settings editor.

The tree picker commits live and the filter browser holds its edits, both
right for what they edit. What was wrong is that `^s` and `←`, learnt on every
other screen, silently did nothing on the picker: same widget, same rows, a
dead key.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fnd.tui import FNDApp
from fnd.tui.settings_screen import TreePickerScreen
from fnd.tui.widgets import COMMIT_KEY
from fnd.tui.widgets.toggle_tree import ToggleGroup, ToggleItem, ToggleTree


def _item(saved: list[list[str]]) -> object:
    from fnd.tui.menu import MenuItem

    group = ToggleGroup(
        "sources",
        "Tag sources",
        (ToggleItem("os", "System tags"), ToggleItem("frontmatter", "Note tags")),
    )
    return MenuItem(
        id="tag_sources",
        label="Tag sources",
        groups_provider=lambda _app: [group],
        picker_getter=lambda _app: ["os"],
        picker_setter=lambda _app, values: saved.append(list(values)),
    )


async def _picker(app: FNDApp, pilot: object) -> list[list[str]]:
    saved: list[list[str]] = []
    app.push_screen(TreePickerScreen(_item(saved)))  # type: ignore[arg-type]
    for _ in range(12):
        await pilot.pause()  # type: ignore[attr-defined]
    return saved


@pytest.mark.parametrize("key", ["escape", COMMIT_KEY.replace("^", "ctrl+"), "left"])
@pytest.mark.asyncio
async def test_every_leaving_gesture_leaves(tmp_index_dir: Path, key: str) -> None:
    app = FNDApp(index_dir=tmp_index_dir)
    async with app.run_test(size=(90, 24)) as pilot:
        await pilot.pause()
        await _picker(app, pilot)
        assert isinstance(app.screen, TreePickerScreen), "the premise"
        app.screen.query_one("#tree_picker", ToggleTree).focus()
        for _ in range(4):
            await pilot.pause()
        if key == "left":
            # ← collapses first and leaves from the outermost level, which is
            # what it does in the filter browser too.
            await pilot.press("left")
            for _ in range(6):
                await pilot.pause()
        await pilot.press(key)
        for _ in range(10):
            await pilot.pause()
        left = app.screen.__class__ is not TreePickerScreen

    assert left, f"{key!r} did nothing on the picker"


@pytest.mark.asyncio
async def test_the_footer_names_the_keys_that_work(tmp_index_dir: Path) -> None:
    """A key that works unadvertised is the mirror of one advertised that
    does not."""
    app = FNDApp(index_dir=tmp_index_dir)
    async with app.run_test(size=(90, 24)) as pilot:
        await pilot.pause()
        await _picker(app, pilot)
        on_screen = "\n".join(
            "".join(s.text for s in strip) for strip in app.screen._compositor.render_strips()
        )

    assert COMMIT_KEY in on_screen, on_screen.splitlines()[-1]
