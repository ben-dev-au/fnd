"""Generation-guarded scroll commit.

The single-anchor controller stopped the *many inline scroll sites* race, but
the strategy's ``_do_scroll_to_chunk`` retry chain reschedules itself across
refreshes — so rapid navigation spawns overlapping chains, each pinned to a
captured chunk seq, all ending in a ``scroll_to_region`` commit. They race;
the loser wins. A monotonic generation token (bumped on ``arm``) lets a
superseded chain die at its next tick AND immediately before the commit
(cooperative cancellation: re-check freshness right before the side effect).
"""

from collections.abc import Callable
from typing import cast

from textual.geometry import Region

from fnd.tui.preview_scroll import (
    PreviewScrollController,
    ScrollAnchor,
    StructuralHost,
    StructuralScrollStrategy,
    ViewportLocation,
)
from tests.test_preview_scroll_controller import _FakeHost, _FakePane, _FakeWidget


class _DeferStrategy:
    """Captures ``on_settled`` without firing it — models a scroll commit that
    lands a tick later, so a newer ``arm`` can supersede it in between."""

    def __init__(self) -> None:
        self.held: list[Callable[[], None]] = []

    def reconcile(
        self,
        anchor: ScrollAnchor,
        on_settled: Callable[[], None] | None = None,
        **_kw: object,
    ) -> None:
        if on_settled is not None:
            self.held.append(on_settled)

    def locate(self) -> ViewportLocation | None:
        return None

    def scroll_to_location(self, location: ViewportLocation, on_done: object = None) -> None:
        return None


def test_stale_reveal_is_honoured_but_does_not_flip_is_settling() -> None:
    strat = _DeferStrategy()
    c = PreviewScrollController(select_strategy=lambda: strat)
    revealed: list[str] = []
    c.arm(ScrollAnchor("p", 1))  # generation 1
    c.reconcile(on_settled=lambda: revealed.append("g1"))
    c.arm(ScrollAnchor("p", 2))  # generation 2 supersedes; gen2 nav in flight
    assert c.is_settling
    strat.held[0]()  # gen1's deferred commit lands LATE
    assert revealed == ["g1"]  # reveal still honoured — no stranded hidden container
    assert c.is_settling  # but the stale commit did NOT open gen2's settling gate


def test_current_reveal_flips_is_settling() -> None:
    strat = _DeferStrategy()
    c = PreviewScrollController(select_strategy=lambda: strat)
    c.arm(ScrollAnchor("p", 1))
    c.reconcile(on_settled=lambda: None)
    strat.held[0]()  # the current generation's commit lands
    assert not c.is_settling


def test_stale_do_scroll_does_not_commit_or_reschedule() -> None:
    # height 0 would normally reschedule the retry chain; a superseded chain must
    # instead die on this tick — no scroll, no reschedule, reveal floor honoured.
    target = _FakeWidget(Region(0, 0, 80, 0))
    pane = _FakePane()
    host = _FakeHost(pane, chunk_widgets={3: target}, match_targets={3: target})
    strat = StructuralScrollStrategy(cast(StructuralHost, host))
    fired: list[bool] = []

    strat._do_scroll_to_chunk(
        3,
        retries=5,
        on_done=lambda: fired.append(True),
        generation=5,
        current_generation=lambda: 9,  # superseded
    )

    assert pane.captured is None
    assert host.deferred == []
    assert fired == [True]


def test_current_do_scroll_commits() -> None:
    inner = _FakeWidget(Region(0, 100, 80, 2))
    target = _FakeWidget(Region(0, 100, 80, 40), first_match_block=inner)
    pane = _FakePane(height=40)
    host = _FakeHost(pane, chunk_widgets={5: target}, match_targets={5: target})
    strat = StructuralScrollStrategy(cast(StructuralHost, host))

    strat._do_scroll_to_chunk(5, margin_from=0.25, generation=7, current_generation=lambda: 7)

    assert pane.captured is not None


def test_pre_commit_guard_catches_supersession_after_resolution() -> None:
    # The target resolves (would commit), but the generation bumps between
    # resolution and the commit — the re-check immediately before the side
    # effect must prevent the scroll (the cooperative-cancel window).
    inner = _FakeWidget(Region(0, 100, 80, 2))
    target = _FakeWidget(Region(0, 100, 80, 40), first_match_block=inner)
    pane = _FakePane(height=40)
    host = _FakeHost(pane, chunk_widgets={5: target}, match_targets={5: target})
    strat = StructuralScrollStrategy(cast(StructuralHost, host))
    reads = {"n": 0}

    def _cur() -> int:
        reads["n"] += 1
        return 7 if reads["n"] <= 1 else 9  # current at entry, stale by commit

    strat._do_scroll_to_chunk(5, generation=7, current_generation=_cur)

    assert pane.captured is None  # pre-commit guard fired
    assert reads["n"] >= 2  # both guard points were consulted
