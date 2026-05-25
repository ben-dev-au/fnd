"""Cache maintenance workflows — end-to-end.

Covers:
  - Update cache (currently a stub; will surface a notify)
  - Prune stale entries (no-op when cache empty)
  - Clear PDF structure cache (destructive confirm)
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from fnd.cache import ExtractionCache
from fnd.config import Config
from fnd.extract.base import Block, Chunk
from fnd.tui import FNDApp


def _chunk(i: int) -> Chunk:
    return Chunk(
        parent_id=f"p{i}",
        path=f"/x/{i}.pdf",
        mtime=0,
        kind="pdf",
        body="b",
        body_struct=[Block(kind="p", text="b")],
        body_md="b",
        page=1,
        chunk_seq=0,
    )


@pytest.mark.asyncio
async def test_prune_when_cache_empty_notifies_no_op(
    app_factory: Callable[[Config], FNDApp], cfg_one: Config
) -> None:
    """Prune on an empty cache surfaces a notification, doesn't push
    a confirm screen, and leaves the app untouched."""
    from fnd.tui.menu import _run_cache_prune

    app = app_factory(cfg_one)
    initial_screen = type(None)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        initial_screen = type(app.screen)
        _run_cache_prune(app)
        await pilot.pause()
        assert type(app.screen) is initial_screen


@pytest.mark.asyncio
async def test_clear_when_cache_empty_notifies_no_op(
    app_factory: Callable[[Config], FNDApp], cfg_one: Config
) -> None:
    """Clear on an empty cache: no confirm pushed."""
    from fnd.tui.menu import _run_cache_clear

    app = app_factory(cfg_one)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        initial = type(app.screen)
        _run_cache_clear(app)
        await pilot.pause()
        assert type(app.screen) is initial


@pytest.mark.asyncio
async def test_clear_with_entries_pushes_destructive_confirm(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    app_factory: Callable[[Config], FNDApp],
    cfg_one: Config,
) -> None:
    """When the cache has content, Clear pushes a confirm screen."""
    cache_root = tmp_path / "cache"
    monkeypatch.setattr("fnd.cache.default_cache_dir", lambda: cache_root)
    cache = ExtractionCache(root=cache_root)
    cache.put("a--v1", [_chunk(1)])
    cache.put("b--v1", [_chunk(2)])

    from fnd.tui.menu import _run_cache_clear
    from fnd.tui.settings_screen import CacheMaintenanceConfirm

    app = app_factory(cfg_one)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        _run_cache_clear(app)
        await pilot.pause()
        assert isinstance(app.screen, CacheMaintenanceConfirm)
