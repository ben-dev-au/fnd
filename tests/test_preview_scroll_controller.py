from fnd.tui.preview_scroll import PreviewScrollController, ScrollAnchor


class FakeStrategy:
    def __init__(self) -> None:
        self.calls: list[ScrollAnchor] = []

    def reconcile(self, anchor: ScrollAnchor) -> None:
        self.calls.append(anchor)


def test_arm_then_reconcile_calls_strategy() -> None:
    strat = FakeStrategy()
    c = PreviewScrollController(select_strategy=lambda: strat)
    a = ScrollAnchor(parent_id="p", focus_chunk_seq=3)
    c.arm(a)
    assert c.is_armed
    c.reconcile()
    assert strat.calls == [a]


def test_reconcile_is_noop_when_released() -> None:
    strat = FakeStrategy()
    c = PreviewScrollController(select_strategy=lambda: strat)
    c.arm(ScrollAnchor(parent_id="p", focus_chunk_seq=0))
    c.release()
    assert not c.is_armed
    c.reconcile()
    assert strat.calls == []


def test_reconcile_idempotent_same_target() -> None:
    strat = FakeStrategy()
    c = PreviewScrollController(select_strategy=lambda: strat)
    a = ScrollAnchor(parent_id="p", focus_chunk_seq=5)
    c.arm(a)
    c.reconcile()
    c.reconcile()
    c.reconcile()
    assert strat.calls == [a, a, a]  # always the same target — order-independent
