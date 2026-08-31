"""The ▲a ▼b border: how many views of the current result hold a match above and
below the one on screen.

Read off the same tiling n/b walk, so the number is both "screenfuls out of
view" and "presses remaining". The walk tests are the point: a count that is
right at every landing can still stall for a press, and only a sequence sees it.
"""

from __future__ import annotations

from fnd.tui.match_navigator import offscreen_views, step_view, view_anchors

VH = 20


def _views(ys: list[int], top: int, vh: int = VH) -> tuple[int, int]:
    return offscreen_views(view_anchors(ys, vh), ys, top, vh)


def test_all_on_screen_is_zero_both_ways() -> None:
    # Viewport [0, 20); every stop inside it, so one view and nothing beyond it.
    assert _views([2, 8, 15], 0) == (0, 0)


def test_one_view_below_the_fold() -> None:
    # Stops 40 and 48 share the second view.
    assert _views([5, 40, 48], 0) == (0, 1)


def test_two_views_below_the_fold() -> None:
    assert _views([5, 40, 90], 0) == (0, 2)


def test_views_above_the_top() -> None:
    assert _views([5, 40, 110], 100) == (2, 0)


def test_views_both_directions() -> None:
    assert _views([10, 60, 200], 50) == (1, 1)


def test_a_match_at_the_fold_is_not_off_screen() -> None:
    """A stop exactly at the viewport bottom is below; one at the top is visible."""
    assert _views([20, 40], 20) == (0, 1)


def test_empty_is_zero() -> None:
    assert _views([], 0) == (0, 0)
    assert offscreen_views([], [], 0, VH) == (0, 0)


# ── Walks ────────────────────────────────────────────────────────────────────
# The 18 stops one 200-row fence produced at vh=30, where the old rules gave
# 5 views against the 7 hops n actually took.
_FENCE = [56, 69, 82, 95, 108, 120, 133, 146, 159, 172, 183, 186, 190, 194, 198, 208, 213, 219]


def _walk(ys: list[int], vh: int) -> list[tuple[int, int]]:
    """``(above, below)`` at each viewport a run of n visits, from the first."""
    anchors = view_anchors(ys, vh)
    top = anchors[0]
    seen = []
    for _ in range(len(anchors) + 1):
        seen.append(offscreen_views(anchors, ys, top, vh))
        if seen[-1][1] == 0:
            return seen
        nxt = step_view(anchors, ys, top, vh, forward=True)
        assert nxt is not None
        top = nxt
    raise AssertionError(f"the walk never reached the last view: {seen}")


def test_a_walk_counts_down_one_per_press() -> None:
    """The invariant no rest-state check can see: each press moves ▼ down by
    exactly one and ▲ up by exactly one, with no stall and no gap."""
    seen = _walk(_FENCE, 30)

    assert [b for _a, b in seen] == list(range(len(seen) - 1, -1, -1)), seen
    assert [a for a, _b in seen] == list(range(len(seen))), seen


def test_a_walk_keeps_the_total_constant() -> None:
    seen = _walk(_FENCE, 30)

    assert {a + b for a, b in seen} == {len(seen) - 1}, seen


def test_the_border_promise_is_the_walk_length() -> None:
    """▼ on arrival promises how many presses remain; keep it, at every size."""
    for vh in range(8, 61, 4):
        anchors = view_anchors(_FENCE, vh)
        if len(anchors) < 2:
            continue
        promised = offscreen_views(anchors, _FENCE, anchors[0], vh)[1]

        assert len(_walk(_FENCE, vh)) - 1 == promised, vh
