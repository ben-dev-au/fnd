"""Clickable Interface toggle: the app seeds its mode from config, applies
it on mount, and the _apply_mouse_capture helper drives the terminal
driver's mouse-reporting hooks (guarded for headless/test drivers)."""

from __future__ import annotations

from pathlib import Path

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


def test_toggle_getter_reads_config() -> None:
    from fnd.tui.menu import _get_clickable_interface

    app = FNDApp(config=Config())
    assert _get_clickable_interface(app) is False
    app._config = Config(defaults=Defaults(clickable_interface=True))
    assert _get_clickable_interface(app) is True


def test_toggle_setter_persists_and_applies_live(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    from fnd.config import load
    from fnd.tui.menu import _set_clickable_interface

    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text("", encoding="utf-8")
    monkeypatch.setattr("fnd.config.default_config_path", lambda: cfg_path)

    applied: list[bool] = []
    app = SimpleNamespace(
        _config=load(cfg_path),
        _apply_mouse_capture=lambda v: applied.append(v),
        _resolve_profile=lambda: None,
        _refresh_status=lambda: None,
    )
    _set_clickable_interface(app, True)  # type: ignore[arg-type]

    assert applied == [True]
    assert load(cfg_path).defaults.clickable_interface is True


def test_preferences_menu_includes_clickable_interface_toggle() -> None:
    from fnd.tui.menu import KIND_TOGGLE, _provider_preferences

    items = _provider_preferences(FNDApp(config=Config()))
    row = next(i for i in items if i.id == "pref.clickable_interface")
    assert row.kind == KIND_TOGGLE
    assert row.label == "Clickable Interface"
    assert "[green]" in row.description
    assert "[red]" in row.description
    # Opts into Rich-markup rendering so those colour tags are applied.
    assert row.description_markup is True
