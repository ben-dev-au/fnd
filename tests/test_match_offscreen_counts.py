"""Pure geometry for the off-screen view indicator: given the current result's
match stops (content-space tops) and the viewport, how many *screenfuls*
("views") above and below the fold hold a match. A view is one n/b hop."""

from __future__ import annotations

from fnd.tui.match_navigator import _view_buckets, offscreen_views


def test_view_buckets_groups_by_viewport() -> None:
    # vh=20: 5 and 8 share a screenful; 40 starts a second; 90 a third.
    assert _view_buckets([5, 8, 40, 90], 20) == 3
    assert _view_buckets([], 20) == 0
    assert _view_buckets([1], 20) == 1


def test_all_on_screen_is_zero_both_ways() -> None:
    # Viewport [0, 20); every stop inside it.
    assert offscreen_views([2, 8, 15], 0, 20, 20) == (0, 0)


def test_one_view_below_the_fold() -> None:
    # Viewport [0, 20); two below-fold stops within one screenful → one view.
    assert offscreen_views([5, 40, 48], 0, 20, 20) == (0, 1)


def test_two_views_below_the_fold() -> None:
    # Viewport [0, 20); below stops 40 and 90 are >1 screenful apart → two views.
    assert offscreen_views([5, 40, 90], 0, 20, 20) == (0, 2)


def test_views_above_the_top() -> None:
    # Scrolled down: viewport [100, 120); two above stops a screenful apart.
    assert offscreen_views([5, 40, 110], 100, 120, 20) == (2, 0)


def test_views_both_directions() -> None:
    # Viewport [50, 70); one screenful above, one inside, one below.
    assert offscreen_views([10, 60, 200], 50, 70, 20) == (1, 1)


def test_bottom_edge_is_offscreen_top_edge_is_visible() -> None:
    # A stop exactly at the bottom edge is below; one exactly at the top edge is
    # visible.
    assert offscreen_views([20, 40], 20, 40, 20) == (0, 1)


def test_empty_is_zero() -> None:
    assert offscreen_views([], 0, 20, 20) == (0, 0)
