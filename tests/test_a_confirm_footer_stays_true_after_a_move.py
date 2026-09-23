"""`⏎ Confirm` leaves the footer once the cursor moves off the affirmative.

Returning the hint from the same call that places the cursor makes the two
agree at mount only. Without a recompute on a move, one `Down` leaves the
footer promising Confirm while Enter cancels.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from rich.text import Text
from textual.widgets import OptionList

from fnd.tui import FNDApp
from fnd.tui.settings_screen import CacheMaintenanceConfirm, UpdateAllConfirm


def _screens() -> list[tuple[str, Any]]:
    return [
        ("update all", lambda: UpdateAllConfirm(collection_names=["papers"])),
        (
            "clear cache",
            lambda: CacheMaintenanceConfirm(
                title="Clear texture cache",
                summary=Text("Deletes every saved texturing."),
                run=lambda: 0,
                confirm_label="Yes, clear it",
                result_label="cleared",
                irreversible=False,
            ),
        ),
    ]


@pytest.mark.parametrize(("name", "make"), _screens())
@pytest.mark.asyncio
async def test_the_footer_does_not_promise_confirm_from_another_row(
    name: str, make: Any, tmp_index_dir: Path
) -> None:
    app = FNDApp(index_dir=tmp_index_dir)
    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        app.push_screen(make())
        for _ in range(15):
            await pilot.pause()
        await pilot.press("down")
        await pilot.pause()
        options = app.screen.query_one("#confirm_list", OptionList)
        landed = options._options[options.highlighted or 0].id
        rows = ["".join(s.text for s in strip) for strip in app.screen._compositor.render_strips()]
        footer = rows[-1]

    assert landed != "yes", f"{name}: precondition, the cursor moved off the affirmative"
    assert "Confirm" not in footer, f"{name}: {footer.strip()!r}"


@pytest.mark.asyncio
async def test_the_footer_still_names_enter(tmp_index_dir: Path) -> None:
    """The control: dropping the promise must not drop the key."""
    app = FNDApp(index_dir=tmp_index_dir)
    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        app.push_screen(UpdateAllConfirm(collection_names=["papers"]))
        for _ in range(15):
            await pilot.pause()
        rows = ["".join(s.text for s in strip) for strip in app.screen._compositor.render_strips()]
        footer = rows[-1]

    assert "⏎" in footer, footer
