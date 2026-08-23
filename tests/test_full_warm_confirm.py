"""The disclosure shown before warming a big file whole.

What the warm costs cannot be predicted from anything known before it starts,
so the estimate goes to the user with the decision. That makes the decline path
part of the contract: the key must be able to do nothing.
"""

from __future__ import annotations

import pytest
from textual.app import App, ComposeResult
from textual.pilot import Pilot
from textual.widgets import Static

from fnd.tui.full_warm_confirm import FullWarmConfirmScreen, estimate_capture_mb


class _Harness(App[None]):
    def compose(self) -> ComposeResult:
        yield Static("host")


async def _answer(pilot: Pilot[None], app: _Harness, key: str) -> list[bool | None]:
    got: list[bool | None] = []
    app.push_screen(
        FullWarmConfirmScreen(name="Design Patterns.pdf", chunks=719, chars=1_195_575),
        callback=got.append,
    )
    await pilot.pause()
    await pilot.press(key)
    await pilot.pause()
    return got


@pytest.mark.asyncio
async def test_escape_declines_the_warm() -> None:
    app = _Harness()
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        assert await _answer(pilot, app, "escape") == [False]


@pytest.mark.asyncio
async def test_the_default_option_accepts() -> None:
    """ "Warm it" is first, so Enter on the untouched list starts the warm."""
    app = _Harness()
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        assert await _answer(pilot, app, "enter") == [True]


@pytest.mark.asyncio
async def test_cancel_declines_the_warm() -> None:
    app = _Harness()
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        got: list[bool | None] = []
        app.push_screen(
            FullWarmConfirmScreen(name="x.pdf", chunks=719, chars=1_195_575),
            callback=got.append,
        )
        await pilot.pause()
        await pilot.press("down")
        await pilot.press("enter")
        await pilot.pause()
        assert got == [False], "the second option is Cancel and must decline"


def test_the_estimate_errs_high_rather_than_low() -> None:
    """The number exists to warn, so it takes the top of the observed 31-87 KB
    per 1000 characters rather than the middle. This file measured ~104 MB."""
    assert estimate_capture_mb(1_195_575) == pytest.approx(104.0, abs=1.5)
    assert estimate_capture_mb(0) == 0.0
