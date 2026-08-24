"""The wait helpers every timing test rests on, tested without an app.

Driven by a stand-in Pilot whose ``pause`` is free — the shape ``safe_pause``
degrades to when Textual's internal screen wait times out on a loaded runner.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from tests._pilot_wait import wait_stable, wait_until


class _FreePilot:
    """A Pilot whose pause costs nothing, as it does under a degraded runner."""

    def __init__(self) -> None:
        self.app: Any = None
        self.pauses = 0

    async def pause(self, delay: float | None = None) -> None:
        self.pauses += 1


@pytest.mark.asyncio
async def test_wait_stable_does_not_call_a_moving_sample_settled() -> None:
    """Identical samples taken microseconds apart are not evidence of stillness.

    Three rounds of a free pause elapse in tens of microseconds, so a sample
    still about to move reads as settled and the caller asserts mid-flight.
    """
    started = time.monotonic()

    def sample() -> int:
        return 0 if time.monotonic() - started < 0.03 else 1

    await wait_stable(_FreePilot(), sample, rounds=3, timeout=5.0)  # type: ignore[arg-type]
    assert sample() == 1, "returned while the sample was still about to move"


@pytest.mark.asyncio
async def test_wait_stable_returns_once_the_sample_really_holds() -> None:
    pilot = _FreePilot()
    await wait_stable(pilot, lambda: 7, rounds=3, timeout=5.0)  # type: ignore[arg-type]
    assert pilot.pauses > 0


@pytest.mark.asyncio
async def test_wait_until_reports_polls_and_a_raising_predicate() -> None:
    """A predicate that raises every round and one that is merely False read
    identically without this, and they want opposite fixes."""

    def boom() -> bool:
        raise RuntimeError("no such widget")

    with pytest.raises(AssertionError) as exc:
        await wait_until(
            _FreePilot(),  # type: ignore[arg-type]
            boom,
            timeout=0.1,
            message="never became true",
        )
    text = str(exc.value)
    assert "polls" in text
    assert "no such widget" in text


@pytest.mark.asyncio
async def test_wait_until_returns_as_soon_as_the_predicate_holds() -> None:
    calls = {"n": 0}

    def ready() -> bool:
        calls["n"] += 1
        return calls["n"] >= 3

    await wait_until(_FreePilot(), ready, timeout=5.0)  # type: ignore[arg-type]
    assert calls["n"] == 3
