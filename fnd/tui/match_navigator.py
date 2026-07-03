"""Intra-file preview match navigation (n / b).

Steps between the match stops of the currently-mounted preview, hopping by
viewport so an off-screen match in the same file is reachable without manual
scrolling. The geometry (which stop is next/prev) is pure and unit-tested;
region resolution + scrolling live on :class:`MatchNavigator` below.
"""

from __future__ import annotations


def _ref_top(ys: list[int], scroll_y: int, last_target: int | None, margin: int) -> int:
    """Top of the reference viewport: the last jump's resulting position during
    a burst (``last_target`` set), else the live scroll top. Using the last
    target's position — not the not-yet-settled live scroll — lets a rapid
    ``n n n`` burst advance screen-by-screen instead of re-picking the same
    stop while the animation catches up. A manual scroll clears ``last_target``
    (see :meth:`MatchNavigator.on_manual_scroll`), so it reverts to live."""
    if last_target is not None and 0 <= last_target < len(ys):
        return ys[last_target] - margin
    return scroll_y


def next_stop_index(
    ys: list[int], scroll_y: int, viewport_h: int, last_target: int | None, margin: int
) -> int:
    """Index of the first stop at/below the reference viewport's bottom edge;
    wraps to 0 when none is below. ``ys`` is the stops' content-space tops,
    sorted ascending."""
    if not ys:
        return 0
    ref_bottom = _ref_top(ys, scroll_y, last_target, margin) + viewport_h
    for i, y in enumerate(ys):
        if y >= ref_bottom:
            return i
    return 0


def prev_stop_index(
    ys: list[int], scroll_y: int, viewport_h: int, last_target: int | None, margin: int
) -> int:
    """Index of the top stop of the screenful immediately above the reference
    viewport; wraps to the last stop when none is above."""
    if not ys:
        return 0
    ref_top = _ref_top(ys, scroll_y, last_target, margin)
    nearest_above: int | None = None
    for i, y in enumerate(ys):
        if y < ref_top:
            nearest_above = i
        else:
            break
    if nearest_above is None:
        return len(ys) - 1
    # Land on the TOP of that previous screenful (symmetry with next's hop),
    # not just the nearest stop above — so a screen with many matches is one
    # press up, mirroring one press down.
    window_top = ys[nearest_above] - (viewport_h - 2 * margin)
    target = nearest_above
    for i in range(nearest_above, -1, -1):
        if ys[i] >= window_top:
            target = i
        else:
            break
    return target
