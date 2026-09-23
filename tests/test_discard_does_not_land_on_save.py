"""`Esc/← Discard` opens a dialog whose cursor does not sit on "Save changes".

Enter, the reflex after a key that already says discard, would write
`kinds = ["md"]` and prune files out of the corpus at the next update.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from textual.widgets import OptionList

from fnd.filters import FilterSpec
from fnd.filters.scan import SourceSample
from fnd.tui import FNDApp
from fnd.tui.settings_screen import FilterBrowserScreen, UnsavedChangesScreen

_SAMPLE = SourceSample(kinds={"md": 3}, tags={"frontmatter": {"keep": 2}})


async def _unsaved(app: FNDApp, pilot: Any, *, with_save: bool) -> str | None:
    app.push_screen(
        UnsavedChangesScreen(
            what="these filters",
            on_save=(lambda: None) if with_save else None,
            on_leave=lambda: None,
        )
    )
    for _ in range(15):
        await pilot.pause()
    lst = app.screen.query_one("#confirm_list", OptionList)
    return lst._options[lst.highlighted or 0].id


@pytest.mark.asyncio
async def test_it_does_not_open_on_save(tmp_index_dir: Path) -> None:
    app = FNDApp(index_dir=tmp_index_dir)
    async with app.run_test(size=(110, 32)) as pilot:
        await pilot.pause()
        landed = await _unsaved(app, pilot, with_save=True)

    assert landed != "save", "one Enter after a key labelled Discard writes the change"


@pytest.mark.asyncio
async def test_it_opens_on_the_row_that_changes_nothing(tmp_index_dir: Path) -> None:
    app = FNDApp(index_dir=tmp_index_dir)
    async with app.run_test(size=(110, 32)) as pilot:
        await pilot.pause()
        landed = await _unsaved(app, pilot, with_save=True)

    assert landed == "stay", landed


@pytest.mark.asyncio
async def test_without_a_save_on_offer_it_is_unchanged(tmp_index_dir: Path) -> None:
    """The control: that route already landed safely and must keep doing so."""
    app = FNDApp(index_dir=tmp_index_dir)
    async with app.run_test(size=(110, 32)) as pilot:
        await pilot.pause()
        landed = await _unsaved(app, pilot, with_save=False)

    assert landed == "stay", landed


@pytest.mark.asyncio
async def test_the_footer_does_not_promise_a_discard_it_only_offers(
    tmp_index_dir: Path,
) -> None:
    """Esc asks; it does not discard. The key that opens a question cannot be
    labelled with one of the answers."""
    app = FNDApp(index_dir=tmp_index_dir)
    async with app.run_test(size=(110, 32)) as pilot:
        await pilot.pause()
        screen = FilterBrowserScreen(
            title="Index filters",
            spec=FilterSpec(),
            gitignore=True,
            fndignore=True,
            sample_provider=lambda _spec: _SAMPLE,
            on_save=lambda *_a: None,
        )
        app.push_screen(screen)
        for _ in range(25):
            await pilot.pause()
        screen._spec = FilterSpec(kinds=("md",))
        screen._render_footer()
        await pilot.pause()
        rows = ["".join(s.text for s in strip) for strip in screen._compositor.render_strips()]
        footer = rows[-1]

    assert "Discard" not in footer, footer
    assert "Esc" in footer, footer
