"""Clickable Interface toggle: the app seeds its mode from config, applies
it on mount, and the _apply_mouse_capture helper drives the terminal
driver's mouse-reporting hooks (guarded for headless/test drivers)."""

from __future__ import annotations

import pytest

from fnd.config import Config, Defaults
from fnd.tui import FNDApp


class _FakeDriver:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def _enable_mouse_support(self) -> None:
        self.calls.append("enable")

    def _disable_mouse_support(self) -> None:
        self.calls.append("disable")


def test_apply_mouse_capture_drives_driver_hooks() -> None:
    app = FNDApp(config=Config())
    fake = _FakeDriver()
    app._driver = fake  # type: ignore[assignment]

    app._apply_mouse_capture(True)
    assert app._clickable_interface is True
    assert fake.calls == ["enable"]

    app._apply_mouse_capture(False)
    assert app._clickable_interface is False
    assert fake.calls == ["enable", "disable"]


def test_apply_mouse_capture_is_safe_without_driver_hooks() -> None:
    app = FNDApp(config=Config())
    app._driver = object()  # type: ignore[assignment]
    app._apply_mouse_capture(True)
    assert app._clickable_interface is True


def test_init_seeds_mode_from_config() -> None:
    off = FNDApp(config=Config())
    assert off._clickable_interface is False
    on = FNDApp(config=Config(defaults=Defaults(clickable_interface=True)))
    assert on._clickable_interface is True


@pytest.mark.asyncio
async def test_mount_applies_configured_mode() -> None:
    app = FNDApp(config=Config(defaults=Defaults(clickable_interface=True)))
    async with app.run_test(size=(80, 24)):
        assert app._clickable_interface is True
