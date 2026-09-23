"""A footer refresh landing after the screen stack emptied killed its caller.

Every read in `_refresh_footer_hints` reaches the active screen, and it can be
called from a resize or a focus change while the app is coming down. The
failure surfaced as `ScreenStackError: No screens on stack` inside unrelated
preview tests (three runs in six of one of them), which read as a flake until
the rate held under isolation.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fnd.tui import FNDApp


def test_it_is_quiet_when_there_is_no_screen(tmp_index_dir: Path) -> None:
    """A never-started app has an empty stack, which is the teardown state."""
    app = FNDApp(index_dir=tmp_index_dir)

    assert not app.screen_stack
    app._refresh_footer_hints()  # must not raise


@pytest.mark.asyncio
async def test_it_still_paints_when_there_is_one(tmp_index_dir: Path) -> None:
    """The control: the early return must not silence the ordinary case."""
    from textual.widgets import Static

    app = FNDApp(index_dir=tmp_index_dir)
    async with app.run_test(size=(110, 30)) as pilot:
        for _ in range(6):
            await pilot.pause()
        app._refresh_footer_hints()
        await pilot.pause()
        painted = str(app.query_one("#footer_hints", Static).content)

    assert painted.strip(), "the footer went blank"
