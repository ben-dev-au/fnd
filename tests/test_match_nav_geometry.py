"""Pure viewport-hop geometry for preview match navigation (n/b).

One greedy tiling of the stops is the whole model: ``view_anchors`` builds it,
``step_view`` walks it in either direction, and the border counts read off it.
The tests that matter here are the WALK ones at the bottom, because every defect
this module has had was a disagreement between two presses, invisible to any
single-press check.
"""

from __future__ import annotations

from fnd.tui.match_navigator import step_view, view_anchors

VH = 20
# stops at content-y 5, 8, 40, 45, 90 — three views: [5,25) [40,60) [90,110)
YS = [5, 8, 40, 45, 90]
ANCHORS = [5, 40, 90]


def test_views_tile_from_the_first_match_they_reach() -> None:
    assert view_anchors(YS, VH) == ANCHORS


def test_stops_within_one_viewport_share_a_view() -> None:
    assert view_anchors([5, 8, 15], VH) == [5]


def test_an_unlaid_out_pane_has_no_views() -> None:
    assert view_anchors(YS, 0) == []
    assert view_anchors([], VH) == []


def test_next_hops_to_the_following_view() -> None:
    assert step_view(ANCHORS, YS, 5, VH, forward=True) == 40


def test_next_wraps_to_first() -> None:
    assert step_view(ANCHORS, YS, 90, VH, forward=True) == 5


def test_prev_hops_to_the_preceding_view() -> None:
    assert step_view(ANCHORS, YS, 90, VH, forward=False) == 40


def test_prev_wraps_to_last() -> None:
    assert step_view(ANCHORS, YS, 5, VH, forward=False) == 90


def test_an_off_tile_viewport_snaps_to_the_next_view() -> None:
    """A manual scroll leaves the viewport between anchors; both directions take
    the nearest one beyond it rather than needing a rule of their own."""
    assert step_view(ANCHORS, YS, 50, VH, forward=True) == 90
    assert step_view(ANCHORS, YS, 50, VH, forward=False) == 40


def test_no_views_means_nowhere_to_go() -> None:
    assert step_view([], [], 0, VH, forward=True) is None
    assert step_view([], [], 0, VH, forward=False) is None


def test_a_lone_view_has_nowhere_to_go() -> None:
    """Everything on screen: both keys report no destination rather than
    re-anchoring onto a match the reader can already see."""
    assert step_view([5], [5], 5, VH, forward=True) is None
    assert step_view([5], [5], 5, VH, forward=False) is None


# ── Walks ────────────────────────────────────────────────────────────────────
# Stops ~13 apart in one long fence: the spacing that tells the hop rules apart.
FENCE = [20, 33, 46, 59, 71, 83, 96, 109, 122, 135, 148, 161]


def _walk(anchors: list[int], stops: list[int], start: int, vh: int, *, forward: bool) -> list[int]:
    """Every viewport a run of n (or b) visits, until it wraps to the start."""
    tops = [start]
    for _ in range(len(anchors) + 1):
        nxt = step_view(anchors, stops, tops[-1], vh, forward=forward)
        assert nxt is not None
        if nxt == start:
            return tops
        tops.append(nxt)
    raise AssertionError(f"the walk never wrapped: {tops}")


def test_n_visits_every_view_once_then_wraps() -> None:
    anchors = view_anchors(FENCE, 40)
    tops = _walk(anchors, FENCE, anchors[0], 40, forward=True)

    assert tops == anchors, f"n visited {tops}, views are {anchors}"


def test_b_retraces_exactly_the_viewports_n_visited() -> None:
    """b must undo n. When it does not, ▲ counts viewports n never shows and the
    border's total changes as you walk."""
    anchors = view_anchors(FENCE, 40)
    down = _walk(anchors, FENCE, anchors[0], 40, forward=True)
    up = _walk(anchors, FENCE, down[-1], 40, forward=False)

    assert len(down) > 2, down
    assert up == down[::-1], f"n visited {down}, b retraced {up}"


def test_one_press_moves_one_view_at_every_viewport_size() -> None:
    """A stride that differs between the two directions only shows up at some
    sizes — the 12-stop fence at vh=40 is where it first did."""
    for vh in range(8, 61, 4):
        anchors = view_anchors(FENCE, vh)
        if len(anchors) < 3:
            continue
        down = _walk(anchors, FENCE, anchors[0], vh, forward=True)
        assert down == anchors, f"vh={vh}: n visited {down}, views are {anchors}"
        assert _walk(anchors, FENCE, down[-1], vh, forward=False) == down[::-1], f"vh={vh}"


def test_a_wrap_onto_a_visible_view_is_no_navigation() -> None:
    """One view, already on screen: n and b must sit still rather than snap the
    pane to the view's top and show the reader what is already in front of them."""
    assert step_view([193], [193], 183, 40, forward=True) is None
    assert step_view([193], [193], 183, 40, forward=False) is None


def test_a_wrap_onto_an_off_screen_view_still_wraps() -> None:
    """The wrap itself is not what was wrong — only wrapping onto a visible view."""
    assert step_view([5, 90], [5, 90], 90, 20, forward=True) == 5
    assert step_view([5, 90], [5, 90], 5, 20, forward=False) == 90
