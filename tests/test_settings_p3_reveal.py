"""Phase 3 (Settings UX redesign) — reveal & open-keybindings tests."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest


def test_reveal_runs_open_r_on_macos(tmp_path: Path) -> None:
    """Spec: Reveal-in-Finder — uses `open -R <path>` on macOS."""
    from fnd import opener

    p = tmp_path / "x.toml"
    p.write_text("")
    with patch.object(subprocess, "Popen") as mock_popen:
        opener.reveal(p)
        mock_popen.assert_called_once()
        args = mock_popen.call_args.args[0]
        assert args[0] == "open"
        assert args[1] == "-R"
        assert args[2] == str(p)


@pytest.fixture
def built_index(fixtures_dir: Path, tmp_index_dir: Path) -> Path:
    from fnd.index import build_index

    build_index(roots=[fixtures_dir], index_dir=tmp_index_dir, collection="default")
    return tmp_index_dir


@pytest.mark.asyncio
async def test_root_has_open_keybindings_file(built_index: Path) -> None:
    """Spec: IA › Root — sibling action for the keybindings TOML."""
    from fnd.tui import FNDApp
    from fnd.tui.settings_screen import SettingsList

    app = FNDApp(index_dir=built_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_open_command_palette()
        await pilot.pause()
        lst = app.screen.query_one(SettingsList)
        labels = [it.label for it in lst._items]
        assert "↗ Keybindings file" in labels


@pytest.mark.asyncio
async def test_shift_enter_on_open_config_calls_reveal(built_index: Path) -> None:
    """Spec: Reveal pattern — Shift+Enter on the Open config row reveals
    config.toml in Finder."""
    from fnd.tui import FNDApp
    from fnd.tui.settings_screen import SettingsList, SettingsScreen

    app = FNDApp(index_dir=built_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_open_command_palette()
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, SettingsScreen)
        # Bridge focus from the filter Input → list (the new on_mount
        # focuses the Input so typing immediately filters).
        await pilot.press("down")
        await pilot.pause()
        lst = screen.query_one(SettingsList)
        idx = next(i for i, it in enumerate(lst._items) if it.id == "root.open_config_file")
        lst.cursor_index = idx
        with patch("fnd.opener.reveal") as mock_reveal:
            await pilot.press("shift+enter")
            await pilot.pause()
            mock_reveal.assert_called_once()
