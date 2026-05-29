"""Centralised preview scroll-to-match.

Single source of truth for where the preview pane is scrolled. Replaces
the scattered inline scroll sites whose overlapping, differing scrolls
raced (last-writer-wins). Every layout/mount event reconciles against
ONE anchor, so call order no longer changes the outcome.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class ScrollAnchor:
    parent_id: str
    focus_chunk_seq: int
    intent: str = "first_match"  # or "chunk_top"
    context_fraction: float = 0.25


class ScrollStrategy(Protocol):
    def reconcile(self, anchor: ScrollAnchor) -> None: ...


class PreviewScrollController:
    """Owns the active anchor and whether it is authoritative (armed).

    arm()       — navigation sets the desired target.
    reconcile() — idempotently scroll to the target via the active
                  strategy; no-op when released.
    release()   — user took scroll control; stop reconciling.
    """

    def __init__(self, select_strategy: Callable[[], ScrollStrategy | None]) -> None:
        self._select_strategy = select_strategy
        self._anchor: ScrollAnchor | None = None
        self._armed = False

    @property
    def is_armed(self) -> bool:
        return self._armed

    @property
    def anchor(self) -> ScrollAnchor | None:
        return self._anchor

    def arm(self, anchor: ScrollAnchor) -> None:
        self._anchor = anchor
        self._armed = True

    def release(self) -> None:
        self._armed = False

    def reconcile(self) -> None:
        if not self._armed or self._anchor is None:
            return
        strategy = self._select_strategy()
        if strategy is not None:
            strategy.reconcile(self._anchor)
