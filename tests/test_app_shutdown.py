"""Shutdown must finish even when background work is mid-flight.

`_on_exit_app` stops the preview pipeline, cancels the prefetch drainer and stops
the stall watch before handing back to Textual. Every step after a raising one is
skipped, so this is a path where an exception does not merely log — it silently
drops the rest of the teardown.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from fnd.tui import FNDApp


@pytest.mark.asyncio
async def test_exiting_while_the_drainer_runs_completes_the_teardown(
    tmp_index_dir: Path,
) -> None:
    """`await`ing a cancelled task raises CancelledError, and that is a
    BaseException — so `suppress(Exception)` lets it through and it takes the
    rest of `_on_exit_app` with it, including `super()._on_exit_app()`.

    The drainer is idle-blocked on its queue here, which is its normal state:
    cancellation is the only way it ever ends.
    """
    app = FNDApp(index_dir=tmp_index_dir, initial_query="anything")
    async with app.run_test(size=(80, 24)):
        drainer = app._prefetch.sink_drainer
        assert drainer is not None, "no drainer to cancel; this would prove nothing"
        assert not drainer.done(), "the drainer already ended; the await cannot raise"
        stall_stopped: list[bool] = []

        class _Watch:
            def stop(self) -> None:
                stall_stopped.append(True)

        app._stall_watch = _Watch()  # type: ignore[assignment]

        # Directly, rather than through app.exit(), so a raise surfaces here
        # instead of being absorbed by Textual's own teardown.
        await app._on_exit_app()

        assert stall_stopped == [True], (
            "teardown stopped before the stall watch — the drainer's "
            "CancelledError escaped and skipped the rest of the shutdown"
        )
        assert app._prefetch.sink_drainer is None
        assert drainer.cancelled() or drainer.done()


@pytest.mark.asyncio
async def test_exiting_twice_does_not_raise(tmp_index_dir: Path) -> None:
    """The drainer reference is cleared, so a second pass must find nothing to
    cancel rather than awaiting an already-finished task a second time."""
    app = FNDApp(index_dir=tmp_index_dir, initial_query="anything")
    async with app.run_test(size=(80, 24)):
        await app._on_exit_app()
        await asyncio.sleep(0)
        await app._on_exit_app()
