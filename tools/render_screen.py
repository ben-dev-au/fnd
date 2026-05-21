"""Render any fnd screen to an SVG snapshot for visual inspection.

Why this exists
---------------

I can't visually iterate on TUI screens without seeing them rendered.
This harness mounts a chosen screen via ``app.run_test()`` and writes
the active screen's SVG to ``tools/snapshots/<name>.svg``. The SVG is
text — readable from a normal file-reading workflow — and accurately
captures layout, sizes, alignment, colour, and overflow.

The snapshot pipeline mirrors how Textual renders for users:

  1. ``app.run_test(size=W,H)`` creates a fake terminal of the chosen
     size. Set width/height to match a realistic user terminal
     (default 120×34).
  2. The script pushes / drives the chosen screen.
  3. ``app.export_screenshot()`` returns an SVG string — exactly what
     pytest-textual-snapshot writes to disk for snapshot tests.
  4. The SVG is saved under ``tools/snapshots/``.

Usage
-----

    uv run python tools/render_screen.py [SCENARIO ...]

Available scenarios are registered in ``SCENARIOS`` below. With no
arguments, every scenario runs.

Adding scenarios
----------------

Each scenario is an async function that takes a Pilot, mounts the
target screen, and returns once the screen is settled. Register it in
``SCENARIOS`` with a short name that becomes the SVG filename.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import cast

# Ensure the repo root is importable. This file lives at
# ``<repo>/tools/render_screen.py`` so the project's ``fnd/`` package
# is one directory up.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from textual.pilot import Pilot  # noqa: E402

OUTPUT_DIR = Path(__file__).parent / "snapshots"
TERMINAL_SIZE = (120, 34)


# ── Scenarios ─────────────────────────────────────────────────────


async def _settings_root(pilot: Pilot[None]) -> None:
    """Settings root — Preferences / Collections / Indexing / Keybindings."""
    from fnd.tui import FNDApp

    app = cast(FNDApp, pilot.app)
    app.action_open_command_palette()
    await pilot.pause()


async def _indexing_screen(pilot: Pilot[None]) -> None:
    """Indexing sub-screen — pdf-structure status, cache rows, toggles."""
    from fnd.tui import FNDApp
    from fnd.tui.menu import SECTION_INDEXING
    from fnd.tui.settings_screen import open_settings_section

    open_settings_section(cast(FNDApp, pilot.app), SECTION_INDEXING)
    await pilot.pause()


async def _structured_pdf_confirm_install(pilot: Pilot[None]) -> None:
    """Confirm screen when pdf-structure is NOT installed — Install path."""
    from fnd.tui.menu import _open_pdf_install_confirm

    _open_pdf_install_confirm(pilot.app)  # type: ignore[arg-type]
    await pilot.pause()


async def _structured_pdf_confirm_uninstall(pilot: Pilot[None]) -> None:
    """Confirm screen when pdf-structure IS installed — Uninstall path.

    Uses a monkey-patched is_extra_installed so the confirm presents
    its uninstall body regardless of the actual venv state."""
    import fnd.extras as extras
    import fnd.tui.menu as menu
    import fnd.tui.settings_screen as settings_screen

    extras.is_extra_installed = lambda _extra: True  # type: ignore[assignment]
    menu._is_pdf_structure_installed = lambda: True  # type: ignore[assignment]
    # StructuredPdfConfirmScreen also asks its own ``_is_installed``
    # at __init__ time — patch via the module-level helper.
    orig_is_installed = settings_screen.StructuredPdfConfirmScreen._is_installed
    settings_screen.StructuredPdfConfirmScreen._is_installed = lambda _self: True  # type: ignore[assignment]
    try:
        from fnd.tui.menu import _open_pdf_install_confirm

        _open_pdf_install_confirm(pilot.app)  # type: ignore[arg-type]
        await pilot.pause()
    finally:
        settings_screen.StructuredPdfConfirmScreen._is_installed = orig_is_installed  # type: ignore[assignment]


async def _extras_progress_running(pilot: Pilot[None]) -> None:
    """ExtrasInstallProgressScreen while a command is running —
    Background/Cancel buttons should be visible."""
    from fnd.tui.extras_install_progress import (
        ExtrasInstallProgressScreen,
        ProgressEvent,
    )

    screen = ExtrasInstallProgressScreen(action_label="Uninstall")
    pilot.app.push_screen(screen)
    await pilot.pause()
    screen._render_event(ProgressEvent(phase="running", cmd_index=0, cmd_total=2))
    for _ in range(4):
        await pilot.pause()


async def _extras_progress_done(pilot: Pilot[None]) -> None:
    """ExtrasInstallProgressScreen after a successful uninstall —
    single Close button. Used to verify terminal-state layout."""
    from fnd.tui.extras_install_progress import (
        ExtrasInstallProgressScreen,
        ProgressEvent,
    )

    screen = ExtrasInstallProgressScreen(action_label="Uninstall")
    pilot.app.push_screen(screen)
    await pilot.pause()
    screen._render_event(ProgressEvent(phase="done", cmd_index=1, cmd_total=1))
    for _ in range(8):
        await pilot.pause()


async def _extras_progress_failed(pilot: Pilot[None]) -> None:
    """ExtrasInstallProgressScreen after a failed uninstall —
    matches the user's exit 2 screenshot."""
    from fnd.tui.extras_install_progress import (
        ExtrasInstallProgressScreen,
        ProgressEvent,
    )

    screen = ExtrasInstallProgressScreen(action_label="Uninstall")
    pilot.app.push_screen(screen)
    await pilot.pause()
    screen._render_event(ProgressEvent(phase="failed", cmd_index=0, cmd_total=2, error="exit 2"))
    # _enter_terminal_state remove_children/mount return AwaitMount
    # objects that resolve on the next tick. Pump the loop until the
    # new Close button is actually mounted before exporting.
    for _ in range(8):
        await pilot.pause()


async def _cache_clear_confirm(pilot: Pilot[None]) -> None:
    """Destructive cache-clear confirm. Red border + Cannot be undone."""
    from fnd.tui.menu import _run_cache_clear

    _run_cache_clear(pilot.app)  # type: ignore[arg-type]
    await pilot.pause()


async def _first_reindex_warning(pilot: Pilot[None]) -> None:
    """First-reindex warning modal. Catches the modal that the user
    saw with broken Start/Cancel buttons and excess prose."""
    from fnd.tui.first_reindex_warning import FirstReindexWarningScreen

    pilot.app.push_screen(FirstReindexWarningScreen(collection="CPL", n_pdfs=43))
    for _ in range(4):
        await pilot.pause()


async def _update_all_confirm(pilot: Pilot[None]) -> None:
    """Update-all-collections confirm with the new layout."""
    from fnd.tui.settings_screen import UpdateAllConfirm

    pilot.app.push_screen(UpdateAllConfirm(collection_names=["papers", "notes", "wine"]))
    for _ in range(4):
        await pilot.pause()


SCENARIOS: dict[str, Callable[[Pilot[None]], Awaitable[None]]] = {
    "settings_root": _settings_root,
    "indexing": _indexing_screen,
    "pdf_confirm_install": _structured_pdf_confirm_install,
    "pdf_confirm_uninstall": _structured_pdf_confirm_uninstall,
    "extras_progress_running": _extras_progress_running,
    "extras_progress_done": _extras_progress_done,
    "extras_progress_failed": _extras_progress_failed,
    "cache_clear_confirm": _cache_clear_confirm,
    "first_reindex_warning": _first_reindex_warning,
    "update_all_confirm": _update_all_confirm,
}


# ── Runner ────────────────────────────────────────────────────────


async def _render(name: str, scenario: Callable[[Pilot[None]], Awaitable[None]]) -> Path:
    """Mount an FNDApp, run the scenario, write the SVG."""
    from fnd.config import Config, Defaults
    from fnd.index import build_index
    from fnd.tui import FNDApp

    fixtures = Path(__file__).parent.parent / "tests" / "fixtures"
    index_dir = OUTPUT_DIR / ".scratch-index" / name
    if not index_dir.exists():
        index_dir.mkdir(parents=True, exist_ok=True)
        build_index(roots=[fixtures], index_dir=index_dir, collection="default")

    cfg = Config(defaults=Defaults())
    app = FNDApp(index_dir=index_dir, config=cfg)
    async with app.run_test(size=TERMINAL_SIZE) as pilot:
        await pilot.pause()
        await scenario(pilot)
        await pilot.pause()
        svg = app.export_screenshot(title=f"fnd · {name}")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    target = OUTPUT_DIR / f"{name}.svg"
    target.write_text(svg)
    return target


def main() -> int:
    names = sys.argv[1:] or list(SCENARIOS)
    unknown = [n for n in names if n not in SCENARIOS]
    if unknown:
        print(f"unknown scenario(s): {unknown}", file=sys.stderr)
        print(f"available: {sorted(SCENARIOS)}", file=sys.stderr)
        return 2
    for name in names:
        scenario = SCENARIOS[name]
        target = asyncio.run(_render(name, scenario))
        print(f"wrote {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
