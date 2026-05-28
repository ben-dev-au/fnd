"""Reading mode hides the sidebar so the preview fills the full terminal
width — a normal terminal text selection then covers only the preview
(clean copy for text-to-speech), and it reads distraction-free. Toggling
again restores the sidebar. Reading mode also owns mouse capture: it
hands the terminal back its mouse on entry (so native selection / TTS
work) and re-captures on exit (so click-to-focus / hover wheel-scroll
return)."""

from __future__ import annotations

import pytest

from fnd.config import Config
from fnd.tui import FNDApp


def _record_mouse_calls(app: FNDApp) -> list[str]:
    """Wrap the live driver's mouse-support hooks so tests can assert that
    reading-mode transitions flip terminal mouse reporting. Swapping the
    whole `_driver` is unsafe — Textual's timer/bell code reads other
    driver attrs (e.g. ``is_headless``) — so we patch only the two hooks
    onto whatever driver ``run_test`` mounted, falling back to no-op stubs
    when the headless driver lacks them."""
    calls: list[str] = []
    driver = app._driver  # type: ignore[attr-defined]
    assert driver is not None

    def _enable() -> None:
        calls.append("enable")

    def _disable() -> None:
        calls.append("disable")

    driver._enable_mouse_support = _enable  # type: ignore[attr-defined]
    driver._disable_mouse_support = _disable  # type: ignore[attr-defined]
    return calls


def test_reading_mode_action_registered() -> None:
    from fnd.tui.actions import REGISTRY

    action = next(a for a in REGISTRY if a.id == "toggle_reading_mode")
    assert action.default_key == "z"
    assert action.footer_label == "Reading View"


@pytest.mark.asyncio
async def test_reading_mode_toggles_sidebar_visibility() -> None:
    app = FNDApp(config=Config())
    async with app.run_test(size=(100, 30)) as pilot:
        calls = _record_mouse_calls(app)

        column = app.query_one("#results_column")
        preview = app.query_one("#preview_pane")
        assert column.display is True
        assert app._reading_mode is False
        assert preview.has_class("-reading") is False

        app.action_toggle_reading_mode()
        await pilot.pause()
        assert app._reading_mode is True
        assert column.display is False
        # Border/padding dropped (via class) so selection copies no frame,
        # and the pane's own scrollbar is zeroed (the inner buffer keeps the
        # match-marker bar) so reading view shows no duplicate scrollbar.
        assert preview.has_class("-reading") is True
        assert preview.styles.scrollbar_size_vertical == 0
        # Mouse capture released so the terminal owns selection / TTS.
        assert calls[-1] == "disable"

        app.action_toggle_reading_mode()
        await pilot.pause()
        assert app._reading_mode is False
        assert column.display is True
        assert preview.has_class("-reading") is False
        assert preview.styles.scrollbar_size_vertical == 1
        # Mouse capture restored on exit → hover wheel-scroll back.
        assert calls[-1] == "enable"


@pytest.mark.asyncio
async def test_escape_exits_reading_mode() -> None:
    app = FNDApp(config=Config())
    async with app.run_test(size=(100, 30)) as pilot:
        calls = _record_mouse_calls(app)

        app.action_toggle_reading_mode()
        await pilot.pause()
        assert app._reading_mode is True
        assert calls[-1] == "disable"

        app.action_escape_back()
        await pilot.pause()
        assert app._reading_mode is False
        assert app.query_one("#results_column").display is True
        # Esc-exit takes the same path through action_toggle_reading_mode,
        # so mouse capture is restored.
        assert calls[-1] == "enable"


def test_apply_mouse_capture_is_safe_without_driver_hooks() -> None:
    """Helper must no-op when the driver lacks the private hooks (headless
    test drivers, future driver changes) instead of raising — reading-mode
    toggling stays usable even in such environments."""
    app = FNDApp(config=Config())
    app._driver = object()  # type: ignore[assignment]
    app._apply_mouse_capture(True)
    app._apply_mouse_capture(False)
