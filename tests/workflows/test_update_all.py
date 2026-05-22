"""Update all collections — end-to-end.

The workflow:
  1. Settings menu drilled into Indexing or Collections.
  2. Select "Update all collections".
  3. Confirm screen pushed; press Enter on Yes.
  4. IndexerScreen mounts and iterates through every collection.
  5. Chain finishes, modal returns to terminal state.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from fnd.config import Config
from fnd.tui import FNDApp

from .conftest import wait_until


@pytest.mark.asyncio
async def test_yes_runs_every_collection(
    app_factory: Callable[[Config], FNDApp], cfg_three: Config
) -> None:
    """Confirm > Yes triggers start_indexer for each queued collection."""
    from fnd.tui.settings_screen import UpdateAllConfirm

    app = app_factory(cfg_three)
    invocations: list[str] = []
    original = app.start_indexer

    def _record(*, collection: str, **kw: object) -> bool:
        invocations.append(collection)
        return original(collection=collection, **kw)  # type: ignore[arg-type]

    app.start_indexer = _record  # type: ignore[method-assign]

    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        # Record the pre-confirm baseline. Auto-resume on launch can
        # call start_indexer for an inherited "default" collection
        # before the test pushes its confirm; that invocation is not
        # part of what this test exercises.
        baseline = len(invocations)
        app.push_screen(UpdateAllConfirm(collection_names=["alpha", "beta", "gamma"]))
        await pilot.pause()
        await pilot.press("enter")

        ok = await wait_until(
            pilot, lambda: len(invocations) - baseline >= 3, timeout=8.0, ticks=80
        )

    triggered = invocations[baseline:]
    assert ok, f"chain didn't fire 3 collections within timeout (saw {triggered})"
    assert triggered == ["alpha", "beta", "gamma"], triggered


@pytest.mark.asyncio
async def test_cancel_does_not_start_chain(
    app_factory: Callable[[Config], FNDApp], cfg_three: Config
) -> None:
    """Confirm > Cancel keeps the app clean (no indexer task)."""
    from fnd.tui.settings_screen import UpdateAllConfirm

    app = app_factory(cfg_three)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.push_screen(UpdateAllConfirm(collection_names=["alpha", "beta"]))
        await pilot.pause()
        # The Yes option is initially focused; arrow down + Enter to
        # land on Cancel.
        await pilot.press("down")
        await pilot.press("enter")
        await pilot.pause()

    assert app._indexer_task is None
    assert app._indexer_chain_remaining == []


@pytest.mark.asyncio
async def test_empty_queue_is_a_noop(
    app_factory: Callable[[Config], FNDApp], cfg_one: Config
) -> None:
    """No collections configured -> Update all should bail cleanly."""
    from fnd.tui.settings_screen import UpdateAllConfirm

    app = app_factory(cfg_one)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        # Confirm with an empty queue; the screen should pop cleanly.
        app.push_screen(UpdateAllConfirm(collection_names=[]))
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()

    assert app._indexer_task is None
