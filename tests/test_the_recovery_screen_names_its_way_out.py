"""`[3] Dismiss` quits fnd when the recovery screen is the whole app.

The screen serves two callers. In-session it pops back to the menu, which is
what "Dismiss" means; at startup `ConfigRecoveryApp` is the only thing running,
so the same row ends the process. The screen's own docstring records both, and
the row said one of them.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from textual.widgets import Static

from fnd.tui.config_recovery_screen import ConfigRecoveryApp, ConfigRecoveryScreen


def _rows(screen: ConfigRecoveryScreen) -> str:
    return " ".join(str(w.render()) for w in screen.query(Static))


@pytest.mark.asyncio
async def test_at_startup_it_says_it_quits(tmp_path: Path) -> None:
    app = ConfigRecoveryApp(error_text="boom", config_path=tmp_path / "config.toml")
    async with app.run_test(size=(100, 30)) as pilot:
        for _ in range(10):
            await pilot.pause()
        screen = app.screen
        assert isinstance(screen, ConfigRecoveryScreen)
        painted = _rows(screen)

    assert "Quit" in painted, painted
    assert "[3] Dismiss" not in painted, painted


@pytest.mark.asyncio
async def test_in_session_it_still_says_dismiss(tmp_path: Path, tmp_index_dir: Path) -> None:
    """The control: from the running app, backing out is not a quit."""
    from fnd.tui import FNDApp

    app = FNDApp(index_dir=tmp_index_dir)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app.push_screen(
            ConfigRecoveryScreen(error_text="boom", config_path=tmp_path / "config.toml")
        )
        for _ in range(10):
            await pilot.pause()
        screen = app.screen
        assert isinstance(screen, ConfigRecoveryScreen)
        painted = _rows(screen)

    assert "Dismiss" in painted, painted
    assert "Quit" not in painted, painted
