"""`^s Apply` on an invalid expression refuses observably.

The status line already shows the parse error, so refreshing it changes no
pixel (14 identical pane captures over 3.5 seconds read as a dead key). The
leaving prompt must not offer "Save changes" for the same text and bounce back
with no explanation. The source form holds the same contract.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from textual.widgets import OptionList, TextArea

from fnd.filters import FilterSpec
from fnd.tui import FNDApp
from fnd.tui.settings_screen import FilterTextScreen, UnsavedChangesScreen


async def _editor(app: FNDApp, pilot: Any, text: str) -> FilterTextScreen:
    app.push_screen(
        FilterTextScreen(title="Index filters (text)", spec=FilterSpec(), on_save=lambda _s: None)
    )
    for _ in range(15):
        await pilot.pause()
    screen = app.screen
    assert isinstance(screen, FilterTextScreen)
    screen.query_one("#filter_text", TextArea).text = text
    for _ in range(8):
        await pilot.pause()
    return screen


def test_invalid_text_blocks_the_save() -> None:
    """The seam the leaving prompt reads."""
    screen = FilterTextScreen(title="t", spec=FilterSpec(), on_save=lambda _s: None)
    screen._parsed = lambda: (None, _Err(7, "unexpected token 'kb'"))  # type: ignore[method-assign]

    assert "col 7" in screen.save_blocked()


class _Err:
    def __init__(self, column: int, message: str) -> None:
        self.column = column
        self.message = message


@pytest.mark.asyncio
async def test_a_refused_apply_says_something(tmp_index_dir: Path) -> None:
    app = FNDApp(index_dir=tmp_index_dir)
    said: list[str] = []
    async with app.run_test(size=(110, 30)) as pilot:
        await pilot.pause()
        app.notify = lambda msg, **kw: said.append(str(msg))  # type: ignore[method-assign]
        screen = await _editor(app, pilot, "file.size < 200kb")
        screen.action_save_close()
        for _ in range(6):
            await pilot.pause()
        still_open = isinstance(app.screen, FilterTextScreen)

    assert still_open, "it must not apply text it cannot parse"
    assert said, "and it must not refuse in silence"
    assert any("Not applied" in m for m in said), said


@pytest.mark.asyncio
async def test_the_leaving_prompt_stops_offering_it(tmp_index_dir: Path) -> None:
    app = FNDApp(index_dir=tmp_index_dir)
    async with app.run_test(size=(110, 30)) as pilot:
        await pilot.pause()
        await _editor(app, pilot, "file.size < 200kb")
        await pilot.press("escape")
        for _ in range(10):
            await pilot.pause()
        prompt = app.screen
        assert isinstance(prompt, UnsavedChangesScreen)
        ids = [o.id for o in prompt.query_one("#confirm_list", OptionList)._options]
        painted = " ".join(
            " ".join(
                "".join(s.text for s in strip).strip().strip("│").strip()
                for strip in app.screen._compositor.render_strips()
            ).split()
        )

    assert "save" not in ids, ids
    assert "Cannot save yet" in painted, painted


@pytest.mark.asyncio
async def test_valid_text_still_saves(tmp_index_dir: Path) -> None:
    """The control: the guard must not block text the screen can parse."""
    saved: list[Any] = []
    app = FNDApp(index_dir=tmp_index_dir)
    async with app.run_test(size=(110, 30)) as pilot:
        await pilot.pause()
        app.push_screen(
            FilterTextScreen(title="t", spec=FilterSpec(), on_save=lambda spec: saved.append(spec))
        )
        for _ in range(15):
            await pilot.pause()
        screen = app.screen
        assert isinstance(screen, FilterTextScreen)
        assert not screen.save_blocked()
        screen.query_one("#filter_text", TextArea).text = "file.size <= 200000"
        for _ in range(8):
            await pilot.pause()
        screen.action_save_close()
        for _ in range(6):
            await pilot.pause()

    assert saved, "valid text must still apply"
    assert saved[0].max_size == 200000
