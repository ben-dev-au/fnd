"""Properties the n/b view model holds for any stop layout and viewport."""

from __future__ import annotations

from itertools import pairwise

from hypothesis import given, settings
from hypothesis import strategies as st

from fnd.tui.match_navigator import offscreen_views, step_view, view_anchors

_stops = st.lists(st.integers(min_value=0, max_value=4000), min_size=0, max_size=60).map(
    lambda ys: sorted(set(ys))
)
_vh = st.integers(min_value=1, max_value=120)


@given(ys=_stops, vh=_vh)
@settings(max_examples=400)
def test_views_are_stops_spaced_at_least_a_viewport_apart(ys: list[int], vh: int) -> None:
    anchors = view_anchors(ys, vh)

    assert all(a in ys for a in anchors)
    assert anchors == sorted(anchors)
    assert all(b - a >= vh for a, b in pairwise(anchors))


@given(ys=_stops, vh=_vh)
@settings(max_examples=400)
def test_every_stop_is_visible_from_some_view(ys: list[int], vh: int) -> None:
    """The tiling may not strand a match: if no view shows it, n/b can never
    bring it on screen, which is the whole defect this module exists to fix."""
    anchors = view_anchors(ys, vh)

    for y in ys:
        assert any(a <= y < a + vh for a in anchors), f"stop {y} is in no view of {anchors}"


@given(ys=_stops, vh=_vh)
@settings(max_examples=400)
def test_n_visits_every_view_exactly_once_then_wraps(ys: list[int], vh: int) -> None:
    anchors = view_anchors(ys, vh)
    if len(anchors) < 2:
        return
    seen = [anchors[0]]
    for _ in range(len(anchors) + 1):
        nxt = step_view(anchors, ys, seen[-1], vh, forward=True)
        assert nxt is not None
        if nxt == anchors[0]:
            break
        seen.append(nxt)

    assert seen == anchors


@given(ys=_stops, vh=_vh)
@settings(max_examples=400)
def test_b_undoes_n_from_every_view(ys: list[int], vh: int) -> None:
    anchors = view_anchors(ys, vh)
    if len(anchors) < 2:
        return
    for a in anchors:
        forward = step_view(anchors, ys, a, vh, forward=True)
        assert forward is not None
        assert step_view(anchors, ys, forward, vh, forward=False) == a


@given(ys=_stops, vh=_vh)
@settings(max_examples=400)
def test_the_counts_are_the_presses_remaining(ys: list[int], vh: int) -> None:
    """Parked on a view, above and below are its index and the views after it, so
    the border and the keys cannot drift apart."""
    anchors = view_anchors(ys, vh)
    for i, a in enumerate(anchors):
        above, below = offscreen_views(anchors, ys, a, vh)

        assert (above, below) == (i, len(anchors) - 1 - i)
        assert above + below == len(anchors) - 1


@given(ys=_stops, vh=_vh)
@settings(max_examples=400)
def test_a_single_view_never_moves(ys: list[int], vh: int) -> None:
    """Everything on screen: both keys must sit still and the border show nothing."""
    anchors = view_anchors(ys, vh)
    if len(anchors) != 1:
        return

    assert offscreen_views(anchors, ys, anchors[0], vh) == (0, 0)
    assert step_view(anchors, ys, anchors[0], vh, forward=True) is None
    assert step_view(anchors, ys, anchors[0], vh, forward=False) is None


@given(ys=_stops, vh=_vh, top=st.integers(min_value=-50, max_value=4200))
@settings(max_examples=400)
def test_a_step_from_anywhere_lands_on_a_view(ys: list[int], vh: int, top: int) -> None:
    """Including viewports a manual scroll left between views, or outside the
    chunk entirely."""
    anchors = view_anchors(ys, vh)
    for forward in (True, False):
        target = step_view(anchors, ys, top, vh, forward=forward)

        assert target is None or target in anchors


@given(ys=_stops, vh=_vh, top=st.integers(min_value=-30, max_value=4200))
@settings(max_examples=500)
def test_the_border_promises_exactly_the_presses_available(
    ys: list[int], vh: int, top: int
) -> None:
    """From ANY viewport, including one a results landing left part-way through a
    view, ▼ is the number of times n moves before it wraps. Counting by view
    index instead reads 0 while a match sits just past the fold."""
    anchors = view_anchors(ys, vh)
    if not anchors:
        return
    below = offscreen_views(anchors, ys, top, vh)[1]

    moves, cur = 0, top
    for _ in range(len(anchors) + 2):
        nxt = step_view(anchors, ys, cur, vh, forward=True)
        if nxt is None or nxt <= cur:
            break  # nowhere further, or the wrap
        moves += 1
        cur = nxt

    assert moves == below, f"border said {below}, n moved {moves} times from {top}"


@given(ys=_stops, vh=_vh, top=st.integers(min_value=-30, max_value=4200))
@settings(max_examples=500)
def test_walking_forward_brings_every_stop_below_on_screen(
    ys: list[int], vh: int, top: int
) -> None:
    """No match may be skipped: pressing n until it wraps must show every stop
    that started below the fold."""
    anchors = view_anchors(ys, vh)
    if not anchors:
        return
    seen: set[int] = {y for y in ys if top <= y < top + vh}
    cur = top
    for _ in range(len(anchors) + 2):
        nxt = step_view(anchors, ys, cur, vh, forward=True)
        if nxt is None or nxt <= cur:
            break
        cur = nxt
        seen |= {y for y in ys if cur <= y < cur + vh}

    missed = [y for y in ys if y >= top + vh and y not in seen]
    assert not missed, f"n never showed {missed} walking down from {top}"


# The landing sits a quarter-viewport above the match, so it is only one of the
# positions the keys step through if the tiling starts there.
_home = st.integers(min_value=-40, max_value=4000)


@given(ys=_stops, vh=_vh, home=_home)
@settings(max_examples=500)
def test_the_landing_is_a_view_when_it_shows_the_first_match(
    ys: list[int], vh: int, home: int
) -> None:
    anchors = view_anchors(ys, vh, home)
    if not ys:
        return
    if home <= ys[0] < home + vh:
        assert anchors[0] == home, "the landing is not the first view"
    else:
        assert anchors == view_anchors(ys, vh), "a landing elsewhere moved the tiling"


@given(ys=_stops, vh=_vh, home=_home)
@settings(max_examples=500)
def test_b_returns_to_the_landing_the_first_press_left(ys: list[int], vh: int, home: int) -> None:
    """The report this closes: n then b showed a different count than the landing
    did, because b could only reach a view and the landing was not one."""
    anchors = view_anchors(ys, vh, home)
    if len(anchors) < 2 or anchors[0] != home:
        return
    forward = step_view(anchors, ys, home, vh, forward=True)
    assert forward is not None

    assert step_view(anchors, ys, forward, vh, forward=False) == home


@given(ys=_stops, vh=_vh, home=_home)
@settings(max_examples=500)
def test_every_stop_is_still_visible_from_some_view_with_a_landing(
    ys: list[int], vh: int, home: int
) -> None:
    anchors = view_anchors(ys, vh, home)
    for y in ys:
        assert any(a <= y < a + vh for a in anchors), f"stop {y} is in no view of {anchors}"


@given(ys=_stops, vh=_vh, home=_home)
@settings(max_examples=500)
def test_the_count_is_the_presses_remaining_from_the_landing(
    ys: list[int], vh: int, home: int
) -> None:
    anchors = view_anchors(ys, vh, home)
    if not anchors:
        return
    below = offscreen_views(anchors, ys, home, vh)[1]
    moves, cur = 0, home
    for _ in range(len(anchors) + 2):
        nxt = step_view(anchors, ys, cur, vh, forward=True)
        if nxt is None or nxt <= cur:
            break
        moves += 1
        cur = nxt

    assert moves == below, f"border said {below}, n moved {moves} times from {home}"
