"""Per-collection Update index workflow — end-to-end.

Triggers Update index for a single collection (no chain). The
chain bookkeeping fields on the app must remain at single-run
values, and the IndexerScreen title must NOT show "(X of Y)"
suffix.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from fnd.config import Config
from fnd.tui import FNDApp

from .conftest import wait_until


@pytest.mark.asyncio
async def test_single_collection_update_does_not_show_chain_suffix(
    app_factory: Callable[[Config], FNDApp], cfg_one: Config
) -> None:
    """A single-collection update shouldn't render '(1 of 1)' in the
    IndexerScreen title."""
    from fnd.tui.indexer_modal import IndexerScreen

    app = app_factory(cfg_one)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        # Skip the first-reindex warning if pdf-structure is installed
        # in this dev env — that's a separate workflow.
        from fnd.tui.first_reindex_warning import mark_seen as _mark_seen

        _mark_seen()
        app._reindex_with_warning_if_needed("default")
        await wait_until(pilot, lambda: isinstance(app.screen, IndexerScreen))
        screen = app.screen
        assert isinstance(screen, IndexerScreen)
        title = screen._title_text()
        # No chain suffix for a single run.
        assert " of " not in title, f"unexpected chain suffix in single-run title: {title}"


@pytest.mark.asyncio
async def test_unknown_collection_does_not_crash(
    app_factory: Callable[[Config], FNDApp], cfg_one: Config
) -> None:
    """Triggering Update index for a non-existent collection must
    fail gracefully (notify), not crash."""
    app = app_factory(cfg_one)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        # Should not raise.
        app._reindex_with_warning_if_needed("does-not-exist")
        await pilot.pause()
        # App still alive.
        assert app._indexer_task is None
