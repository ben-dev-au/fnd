"""Esc on an invalid expression refuses observably, and never traps the user.

The status line already shows the parse error, so refreshing it changes no
pixel (14 identical pane captures over 3.5 seconds read as a dead key). The
text is a part of the browser: it is never passed on broken, and a second Esc
abandons it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from textual.widgets import TextArea

from fnd.filters import FilterSpec
from fnd.tui import FNDApp
from fnd.tui.settings_screen import FilterTextScreen


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


def test_invalid_text_is_not_handed_back() -> None:
    """The seam the leaving gate reads: an error, and nothing passed on."""
    got: list[Any] = []
    screen = FilterTextScreen(title="t", spec=FilterSpec(), on_save=got.append)
    screen._parsed = lambda: (None, _Err(7, "unexpected token 'kb'"))  # type: ignore[method-assign]
    screen.query_one = lambda *_a, **_k: type("T", (), {"text": "file.size < 200kb"})()  # type: ignore[method-assign]

    assert "col 7" in screen.hand_back()
    assert not got


class _Err:
    def __init__(self, column: int, message: str) -> None:
        self.column = column
        self.message = message


@pytest.mark.asyncio
async def test_a_refused_esc_says_something(tmp_index_dir: Path) -> None:
    app = FNDApp(index_dir=tmp_index_dir)
    said: list[str] = []
    async with app.run_test(size=(110, 30)) as pilot:
        await pilot.pause()
        app.notify = lambda msg, **kw: said.append(str(msg))  # type: ignore[method-assign]
        await _editor(app, pilot, "file.size < 200kb")
        await pilot.press("escape")
        for _ in range(6):
            await pilot.pause()
        still_open = isinstance(app.screen, FilterTextScreen)

    assert still_open, "it must not pass on text it cannot parse"
    assert said, "and it must not refuse in silence"
    assert any("col" in m and "Esc again" in m for m in said), said


@pytest.mark.asyncio
async def test_a_second_esc_abandons_the_typing(tmp_index_dir: Path) -> None:
    """Holding is not trapping: the vocabulary's Cancel drops what is being typed."""
    got: list[Any] = []
    app = FNDApp(index_dir=tmp_index_dir)
    async with app.run_test(size=(110, 30)) as pilot:
        await pilot.pause()
        app.push_screen(FilterTextScreen(title="t", spec=FilterSpec(), on_save=got.append))
        for _ in range(15):
            await pilot.pause()
        screen = app.screen
        screen.query_one("#filter_text", TextArea).text = "file.size < 200kb"
        for _ in range(6):
            await pilot.pause()
        await pilot.press("escape")
        for _ in range(4):
            await pilot.pause()
        await pilot.press("escape")
        for _ in range(6):
            await pilot.pause()
        gone = app.screen is not screen

    assert gone
    assert not got


@pytest.mark.asyncio
async def test_valid_text_still_goes_back(tmp_index_dir: Path) -> None:
    """The control: the hold must not block text the screen can parse."""
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
        screen.query_one("#filter_text", TextArea).text = "file.size <= 200000"
        for _ in range(8):
            await pilot.pause()
        await pilot.press("escape")
        for _ in range(6):
            await pilot.pause()

    assert saved, "valid text must still be carried back"
    assert saved[0].max_size == 200000
