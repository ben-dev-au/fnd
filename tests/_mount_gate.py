"""Park a structural preview mount at ``cancel_task_on``, its early await. It gets
there only after awaiting ``remove_children`` and ``mount``, an unbounded number of
loop turns under load, so wait on ``wait_parked``, never on a pause count."""

from __future__ import annotations

import asyncio
from typing import Any

from textual.pilot import Pilot

from tests._pilot_wait import wait_until


class MountGate:
    """Stands in for ``app._prefetch.cancel_task_on``: every mount parks there until released."""

    def __init__(self, app: Any) -> None:
        self._open = asyncio.Event()
        self._parked: list[object] = []
        app._prefetch.cancel_task_on = self._park

    async def _park(self, container: object) -> None:
        self._parked.append(container)
        await self._open.wait()

    def parked(self, container: object) -> bool:
        return any(c is container for c in self._parked)

    async def wait_parked(self, pilot: Pilot[None], container: object) -> None:
        await wait_until(
            pilot,
            lambda: self.parked(container),
            message="setup: the mount never reached its early await",
        )

    def release(self) -> None:
        self._open.set()
