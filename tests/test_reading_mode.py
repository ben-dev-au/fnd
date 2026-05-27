"""Reading mode hides the sidebar so the preview fills the full terminal
width — a normal terminal text selection then covers only the preview
(clean copy for text-to-speech), and it reads distraction-free. Toggling
again restores the sidebar."""

from __future__ import annotations

import pytest

from fnd.config import Config
from fnd.tui import FNDApp


def test_reading_mode_action_registered() -> None:
    from fnd.tui.actions import REGISTRY

    action = next(a for a in REGISTRY if a.id == "toggle_reading_mode")
    assert action.default_key == "z"
    assert action.footer_label == "Reading View"


@pytest.mark.asyncio
async def test_reading_mode_toggles_sidebar_visibility() -> None:
    app = FNDApp(config=Config())
    async with app.run_test(size=(100, 30)) as pilot:
        column = app.query_one("#results_column")
        preview = app.query_one("#preview_pane")
        assert column.display is True
        assert app._reading_mode is False
        assert preview.has_class("-reading") is False

        app.action_toggle_reading_mode()
        await pilot.pause()
        assert app._reading_mode is True
        assert column.display is False
        # Border/padding dropped (via class) so selection copies no frame.
        assert preview.has_class("-reading") is True

        app.action_toggle_reading_mode()
        await pilot.pause()
        assert app._reading_mode is False
        assert column.display is True
        assert preview.has_class("-reading") is False
