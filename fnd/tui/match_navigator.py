"""Intra-file preview match navigation (n / b).

Steps between the match stops of the currently-mounted preview, hopping by
viewport so an off-screen match in the same file is reachable without manual
scrolling. The geometry (which stop is next/prev) is pure and unit-tested;
region resolution + scrolling live on :class:`MatchNavigator` below.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from textual.containers import VerticalScroll

    from fnd.tui.app import FNDApp


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


class MatchNavigator:
    """Owns intra-file match navigation for the live preview pane.

    Stops are stored as **content-space** tops (stable across scrolling) —
    screen-space regions go stale the moment the pane scrolls. ``next``/``prev``
    pick a stop by viewport geometry and scroll the pane straight to it,
    reusing the enumerator's exact cell/block region math.
    """

    def __init__(self, app: FNDApp) -> None:
        self._app = app
        self._pane: VerticalScroll | None = None
        self._stops: list[int] = []
        self._last_target: int | None = None
        self._margin = 4

    def rebuild(self) -> None:
        """Re-enumerate the current preview's match stops as content-space
        tops. Called when a preview finishes mounting or the query changes;
        clears the burst memory and refreshes the footer indicator."""
        from textual.containers import VerticalScroll

        from fnd.tui.preview_scroll import enumerate_stop_regions

        self._last_target = None
        try:
            pane = self._app.query_one("#preview_pane", VerticalScroll)
        except Exception:
            self._pane, self._stops = None, []
            self._notify()
            return
        self._pane = pane
        spec = self._app._effective_match_spec
        regions = [] if spec.is_empty else enumerate_stop_regions(pane, spec)
        base = pane.scrollable_content_region.offset.y
        oy = pane.scroll_offset.y
        self._stops = sorted(r.y - base + oy for r in regions)
        self._notify()

    @property
    def count(self) -> int:
        return len(self._stops)

    @property
    def position(self) -> int | None:
        """1-based index of the stop last jumped to, or ``None`` before any
        jump (or after a manual scroll)."""
        return None if self._last_target is None else self._last_target + 1

    def on_manual_scroll(self) -> None:
        """Drop the burst memory so the next ``n``/``b`` is computed purely from
        the on-screen position, never resuming from the previous jump."""
        self._last_target = None

    def next(self) -> None:
        self._go(forward=True)

    def prev(self) -> None:
        self._go(forward=False)

    def _go(self, *, forward: bool) -> None:
        if self._pane is None or not self._stops:
            return
        scroll_y = self._pane.scroll_offset.y
        vh = self._pane.scrollable_content_region.height
        chooser = next_stop_index if forward else prev_stop_index
        k = chooser(self._stops, scroll_y, vh, self._last_target, self._margin)
        self._last_target = k
        self._scroll_to_stop(k, vh)
        self._notify()

    def _scroll_to_stop(self, k: int, vh: int) -> None:
        from textual.geometry import Region

        pane = self._pane
        if pane is None:
            return
        # Drop the match ~a quarter down the viewport for context above it.
        margin = int(vh * 0.25)
        region = Region(0, max(0, self._stops[k] - margin), 1, vh)
        # Flag this as a controller-owned scroll so the scroll watcher doesn't
        # treat it as a user scroll (which would clear the burst memory we just
        # set). Mirrors StructuralScrollStrategy's reconcile-scroll guard.
        # ``immediate`` (not animated) so the scroll commits synchronously
        # inside the reconcile window — an animated scroll's watcher trips fire
        # after ``end_reconcile_scroll``, outside the guard, and would then be
        # mistaken for a user scroll.
        preview = getattr(self._app, "_preview", None)
        if preview is not None:
            preview.begin_reconcile_scroll()
        try:
            pane.scroll_to_region(region, top=True, animate=False, immediate=True)
        finally:
            if preview is not None:
                preview.end_reconcile_scroll()

    def _notify(self) -> None:
        with contextlib.suppress(Exception):
            self._app._refresh_footer_hints()
