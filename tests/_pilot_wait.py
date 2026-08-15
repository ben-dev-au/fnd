"""Event-driven Pilot wait helpers.

Under heavy suite load, ``pilot.pause()`` can hit Textual's internal
30 s ``_wait_for_screen`` timeout because per-widget ``call_later``
drains stack up faster than the CPU can flush them. Fixed-duration
``pilot.pause(N)`` makes it worse: it sleeps wall-clock, then still
calls ``_wait_for_screen`` after.

These helpers wrap ``pilot.pause()`` so timeouts are swallowed and
state changes are awaited via predicate polling on a wall-clock
budget — robust to load spikes that exceed the 30 s internal bound.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

from textual.pilot import Pilot, WaitForScreenTimeout


async def safe_pause(pilot: Pilot[None]) -> None:
    """``pilot.pause()`` that swallows ``WaitForScreenTimeout``.

    Use at test setup points where draining is desired but a load
    spike must not crash the test. Falls back to a short idle ladder
    if the underlying pause times out.
    """
    try:
        await pilot.pause()
    except WaitForScreenTimeout:
        for _ in range(8):
            await asyncio.sleep(0)


async def safe_press(pilot: Pilot[None], *keys: str) -> None:
    """``pilot.press(*keys)`` that swallows ``WaitForScreenTimeout``.

    ``pilot.press`` posts keys via ``_app._press_keys`` then calls
    ``_wait_for_screen`` directly — that 30 s timeout is the same load
    flake source we work around in ``safe_pause``.
    """
    try:
        await pilot.press(*keys)
    except WaitForScreenTimeout:
        # Keys were posted; just yield so the app drains them.
        for _ in range(8):
            await asyncio.sleep(0)


async def settle(pilot: Pilot[None], ticks: int = 4) -> None:
    """Repeated ``safe_pause`` to drain the message loop. ``ticks``
    matches the legacy ``for _ in range(n): await pilot.pause()``
    shape — each iteration tolerates a ``WaitForScreenTimeout``."""
    for _ in range(max(1, ticks)):
        await safe_pause(pilot)


async def wait_until(
    pilot: Pilot[None],
    predicate: Callable[[], bool | Awaitable[bool]],
    *,
    timeout: float = 10.0,
    poll: float = 0.02,
    message: str = "",
) -> None:
    """Poll ``predicate`` until truthy or ``timeout`` (wall-clock) elapses.

    Each iteration: evaluate predicate, then yield the event loop via
    ``safe_pause`` (idle drain) or a small sleep so background tasks
    and timers can advance.
    """
    deadline = time.monotonic() + timeout
    iters = 0
    while True:
        try:
            result = predicate()
            if asyncio.iscoroutine(result):
                result = await result  # type: ignore[assignment]
            if result:
                return
        except Exception:
            pass
        if time.monotonic() >= deadline:
            raise AssertionError(
                f"wait_until timed out after {timeout}s: {message or 'predicate stayed False'}"
            )
        # Alternate idle drains and short sleeps. ``safe_pause`` flushes
        # call_later queues (when load allows); ``sleep(poll)`` keeps
        # progress when the drain itself times out.
        if iters % 2 == 0:
            await safe_pause(pilot)
        else:
            await asyncio.sleep(poll)
        iters += 1


async def run_search(pilot: Pilot[None], app: Any, query: str) -> None:
    """Issue a query and wait for it to land.

    ``SearchController.run`` returns as soon as the worker is dispatched, so
    the old ``run(q)`` + one ``pilot.pause()`` under-waits. Gate on the
    controller's own ``idle`` signal — a product signal, not a tick count.
    """
    app._search.run(query)
    await wait_until(
        pilot,
        lambda: app._search.idle,
        message=f"search for {query!r} never committed",
    )
