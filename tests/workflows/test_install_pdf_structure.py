"""Install pdf-structure workflow — end-to-end-ish.

We don't actually run uv pip install in tests (it would hit the
network and take minutes). Instead we exercise the parts the user
reported broken:

  1. The pre-install repair sweep clears orphan dist-info dirs that
     would otherwise make ``uv pip install`` exit 1.
  2. The confirm screen pushes its disclosure.
  3. The progress modal renders without crashing.
  4. The install command list points at fnd's actual python so the
     packages land where fnd reads from on next launch.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from fnd.config import Config
from fnd.tui import FNDApp


def test_install_commands_use_group_sync_in_project_venv() -> None:
    """Inside a uv-managed project venv install_commands resolves to
    ``uv sync --group pdf-structure``. The sync targets the venv that
    owns sys.executable by construction, which is also fnd's runtime
    venv, so the install lands where fnd reads from on next launch."""
    from fnd.extras import PDF_STRUCTURE, _project_pyproject_for_python, install_commands

    assert _project_pyproject_for_python(sys.executable) is not None, (
        "test must run inside the project venv; was sys.executable redirected?"
    )

    cmds = install_commands(PDF_STRUCTURE)
    sync_cmd = next(c for c in cmds if c[:2] == ["uv", "sync"])
    assert sync_cmd == ["uv", "sync", "--group", "pdf-structure"]


def test_pre_install_clears_orphan_dist_info(tmp_path: Path) -> None:
    """The repair sweep removes any dist-info dir missing METADATA,
    even when METADATA 2 (macOS Finder duplicate) is present."""
    from fnd.tui.extras_install_progress import repair_orphan_dist_info

    site = tmp_path / "site-packages"
    site.mkdir()
    broken = site / "tabulate-0.10.0.dist-info"
    broken.mkdir()
    (broken / "METADATA 2").write_text("# stale macOS dup")

    cleaned = repair_orphan_dist_info(site)
    assert cleaned == ["tabulate-0.10.0.dist-info"]
    assert not broken.exists()


@pytest.mark.asyncio
async def test_confirm_screen_mounts(
    app_factory: Callable[[Config], FNDApp], cfg_one: Config
) -> None:
    """Open the Install pdf-structure row from the Indexing menu and
    verify the confirm screen lands without crashing."""
    from fnd.tui.menu import _open_pdf_install_confirm
    from fnd.tui.settings_screen import StructuredPdfConfirmScreen

    app = app_factory(cfg_one)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        _open_pdf_install_confirm(app)
        await pilot.pause()
        assert isinstance(app.screen, StructuredPdfConfirmScreen)


@pytest.mark.asyncio
async def test_progress_modal_handles_failed_event(
    app_factory: Callable[[Config], FNDApp], cfg_one: Config
) -> None:
    """The user reported 'Install failed. exit 1' — the modal must
    not crash on a failed event and must offer Close to dismiss."""
    from textual.widgets import OptionList

    from fnd.tui.extras_install_progress import (
        ExtrasInstallProgressScreen,
        ProgressEvent,
    )

    app = app_factory(cfg_one)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        screen = ExtrasInstallProgressScreen(action_label="Install")
        app.push_screen(screen)
        await pilot.pause()
        screen._render_event(
            ProgressEvent(phase="failed", cmd_index=0, cmd_total=2, error="exit 1")
        )
        # Toggle to terminal state needs a tick to settle the CSS.
        for _ in range(6):
            await pilot.pause()
            await asyncio.sleep(0)

        # Close OptionList is now visible; Background/Cancel is hidden.
        terminal = screen.query_one("#extras_actions_terminal", OptionList)
        assert not terminal.has_class("-hidden")
        running = screen.query_one("#extras_actions_running", OptionList)
        assert running.has_class("-hidden")
