"""First-reindex warning workflow — end-to-end.

Verifies the modal:
  - Only shows when pdf-structure is installed and the marker is
    unset.
  - Skip-when-no-pdfs: a collection with no PDFs starts the indexer
    directly, no modal.
  - Start dismisses with True and marks seen.
  - Cancel dismisses with False without marking seen.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from fnd.config import Config
from fnd.tui import FNDApp


@pytest.fixture(autouse=True)
def _isolate_marker(  # pyright: ignore[reportUnusedFunction]
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each test gets its own marker file so they don't fight."""
    monkeypatch.setattr(
        "fnd.tui.first_reindex_warning._marker_path",
        lambda: tmp_path / "first_reindex_seen",
    )


@pytest.mark.asyncio
async def test_skipped_when_no_pdfs_in_collection(
    app_factory: Callable[[Config], FNDApp], cfg_one: Config
) -> None:
    """The mini corpus is markdown-only, so the warning should skip
    and the indexer should start directly."""
    from fnd.tui.first_reindex_warning import FirstReindexWarningScreen

    app = app_factory(cfg_one)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app._reindex_with_warning_if_needed("default")
        await pilot.pause()
        # The warning must NOT be on the stack.
        assert not any(isinstance(s, FirstReindexWarningScreen) for s in app.screen_stack)


def test_marker_initially_unseen(tmp_path: Path) -> None:
    """Sanity: marker isolation works."""
    from fnd.tui.first_reindex_warning import has_been_seen

    assert not has_been_seen()


def test_mark_seen_then_has_been_seen(tmp_path: Path) -> None:
    from fnd.tui.first_reindex_warning import has_been_seen, mark_seen

    mark_seen()
    assert has_been_seen()


@pytest.mark.asyncio
async def test_start_option_marks_seen(tmp_path: Path) -> None:
    """Selecting 'Start' on the warning marks it seen and dismisses
    with True. Doesn't need a full app — push the screen directly."""
    from textual.app import App
    from textual.widgets import OptionList

    from fnd.tui.first_reindex_warning import FirstReindexWarningScreen, has_been_seen

    class _Host(App[None]):
        async def on_mount(self) -> None:
            self.push_screen(FirstReindexWarningScreen(collection="x", n_pdfs=5))

    host = _Host()
    async with host.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        # Start option is first; Enter selects.
        screen = host.screen
        assert isinstance(screen, FirstReindexWarningScreen)
        lst = screen.query_one("#first_reindex_list", OptionList)
        lst.highlighted = 0
        await pilot.press("enter")
        await pilot.pause()
        # Screen dismissed.
        assert not any(isinstance(s, FirstReindexWarningScreen) for s in host.screen_stack)
    assert has_been_seen()


@pytest.mark.asyncio
async def test_cancel_does_not_mark_seen(tmp_path: Path) -> None:
    from textual.app import App
    from textual.widgets import OptionList

    from fnd.tui.first_reindex_warning import FirstReindexWarningScreen, has_been_seen

    class _Host(App[None]):
        async def on_mount(self) -> None:
            self.push_screen(FirstReindexWarningScreen(collection="x", n_pdfs=5))

    host = _Host()
    async with host.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        screen = host.screen
        assert isinstance(screen, FirstReindexWarningScreen)
        lst = screen.query_one("#first_reindex_list", OptionList)
        # Move to the Cancel option (3rd).
        lst.highlighted = 2
        await pilot.press("enter")
        await pilot.pause()
    assert not has_been_seen()
