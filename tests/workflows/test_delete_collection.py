"""Delete collection workflow — end-to-end."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from fnd.config import Config
from fnd.tui import FNDApp

from .conftest import wait_until


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


@pytest.mark.asyncio
async def test_delete_drops_index_off_main_thread(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    app_factory: Callable[[Config], FNDApp],
    cfg_three: Config,
    built_index: Path,
) -> None:
    """The Tantivy commit + wait_merging_threads (measured 95-145ms on a
    fresh index, seconds on a fragmented one) must run on a worker, not the
    event loop. Confirm: the index ops execute off the main thread, the
    collection's chunks are actually dropped, and the screens pop back."""
    import threading

    from fnd import index as index_mod
    from fnd.query import Searcher
    from fnd.tui.settings_screen import DeleteCollectionScreen

    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text("")
    monkeypatch.setattr("fnd.config.default_config_path", lambda: cfg_path)

    seen: dict[str, object] = {}
    real_ensure = index_mod._ensure_index

    def _spy_ensure(index_dir: Path, **kw: object) -> object:
        seen["thread"] = threading.current_thread()
        return real_ensure(index_dir, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(index_mod, "_ensure_index", _spy_ensure)

    # 'alpha' is the only collection actually indexed in built_index.
    assert Searcher(index_dir=built_index).search("body", collection="alpha")

    app = app_factory(cfg_three)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        # Mirror the real stack: base → per-collection screen → delete confirm,
        # so the Yes branch's two pops return to the base.
        from textual.screen import Screen

        app.push_screen(Screen())
        await pilot.pause()
        app.push_screen(DeleteCollectionScreen(collection_name="alpha"))
        await pilot.pause()
        await pilot.press("enter")  # Yes is the first option
        ok = await wait_until(
            pilot,
            lambda: "thread" in seen and not isinstance(app.screen, DeleteCollectionScreen),
        )
        assert ok, "delete worker never finished / screens never popped"
        await app.workers.wait_for_complete()
        await pilot.pause()

    assert seen["thread"] is not threading.main_thread(), "index drop ran on the event loop"
    assert Searcher(index_dir=built_index).search("body", collection="alpha") == []
    assert "alpha" not in app._config.collections  # type: ignore[union-attr]
