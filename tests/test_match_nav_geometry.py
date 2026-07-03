"""Pure viewport-hop geometry for preview match navigation (n/b)."""

from __future__ import annotations

from fnd.tui.match_navigator import next_stop_index, prev_stop_index

VH, M = 20, 4
# stops at content-y 5, 8, 40, 45, 90 — three "screens": [5,8] [40,45] [90]
YS = [5, 8, 40, 45, 90]


def test_next_hops_to_first_below_viewport() -> None:
    # viewport top 0 → bottom 20; first stop ≥ 20 is 40 (index 2)
    assert next_stop_index(YS, 0, VH, None, M) == 2


def test_next_from_last_target_hops_by_viewport_not_by_match() -> None:
    # just jumped to index 2 (y=40); its screen ~[36,56); next ≥ 56 is 90 (4)
    assert next_stop_index(YS, 0, VH, 2, M) == 4


def test_next_wraps_to_first() -> None:
    assert next_stop_index(YS, 80, VH, 4, M) == 0


def test_prev_hops_up_a_screen() -> None:
    # live scroll top 80 → previous screenful's top stop is 40 (index 2)
    assert prev_stop_index(YS, 80, VH, None, M) == 2


def test_prev_wraps_to_last() -> None:
    assert prev_stop_index(YS, 0, VH, None, M) == len(YS) - 1


def test_empty_is_zero() -> None:
    assert next_stop_index([], 0, VH, None, M) == 0
    assert prev_stop_index([], 0, VH, None, M) == 0
