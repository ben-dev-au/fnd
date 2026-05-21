"""Delete collection workflow — end-to-end."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from fnd.config import Config
from fnd.tui import FNDApp


@pytest.mark.asyncio
async def test_delete_confirm_pushes_destructive_screen(
    app_factory: Callable[[Config], FNDApp], cfg_three: Config
) -> None:
    from fnd.tui.settings_screen import DeleteCollectionScreen

    app = app_factory(cfg_three)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.push_screen(DeleteCollectionScreen(collection_name="beta"))
        await pilot.pause()
        assert isinstance(app.screen, DeleteCollectionScreen)


@pytest.mark.asyncio
async def test_delete_cancel_keeps_collection(
    tmp_path: object,
    monkeypatch: pytest.MonkeyPatch,
    app_factory: Callable[[Config], FNDApp],
    cfg_three: Config,
) -> None:
    """Cancel on the confirm should leave the config untouched."""
    from pathlib import Path as _Path

    cfg_path = _Path(str(tmp_path)) / "config.toml"
    cfg_path.write_text("")
    monkeypatch.setattr("fnd.config.default_config_path", lambda: cfg_path)
    from fnd.tui.settings_screen import DeleteCollectionScreen

    app = app_factory(cfg_three)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.push_screen(DeleteCollectionScreen(collection_name="beta"))
        await pilot.pause()
        # Yes is first; arrow down to Cancel, Enter to select.
        await pilot.press("down")
        await pilot.press("enter")
        await pilot.pause()
        # 'beta' should still be in the config.
        assert "beta" in app._config.collections  # type: ignore[union-attr]
