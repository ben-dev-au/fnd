"""One widget for a whole document, painted from cached strips.

Both preview substrates are the same widget at heart: a ``ScrollView`` holding
one :class:`~textual.strip.Strip` per visual row, serving the viewport through
the line API so cost is bounded by terminal height rather than document size.
They differ only in where the strips come from — wrapped text lines for the flat
(PDF/TXT) path, captured widget output for the frozen markdown path — and in how
a row is addressed across a reflow.

That difference is the whole reason this is a base class rather than a shared
concrete widget. The flat path rebuilds its strips by re-rendering
``FileView.lines`` at the new width; doing that to a frozen document would
re-render captured markdown as plain text and silently discard tables, fences
and highlighting. So the width-rebuild is a subclass hook, never inherited
behaviour, and the base can offer no path that reaches the wrong one.

What lives here is what genuinely does not care about the substrate: the extent,
the viewport paint, the scroll-with-retries that survives being called before
layout, the scrollbar match markers, and multi-line selection.
"""

from __future__ import annotations

from typing import Any

from rich.segment import Segment
from rich.style import Style
from textual import events
from textual.geometry import Size
from textual.scroll_view import ScrollView
from textual.scrollbar import ScrollBar
from textual.strip import Strip

from fnd.tui.preview_scrollbar import MatchAwareScrollBar

__all__ = ["StripDocumentView"]


class StripDocumentView(ScrollView, can_focus=True):
    """A scrollable document of pre-rendered strips.

    Subclasses own their strips and their addressing, and supply:

    * :meth:`_visual_row` — address -> visual row. An *address* is whatever
      survives a reflow for that substrate: a wrap-stable logical line for the
      flat buffer, a document row for frozen captures.
    * :meth:`_ready_for_scroll` — whether an address can be resolved yet.
    * :meth:`_rebuild_for_width` — re-wrap, re-freeze, or nothing.
    * :attr:`match_rows`, :meth:`row_of_chunk`, :meth:`first_match_row_of_chunk`.
    * :meth:`_row_overlay_style` — optional per-row accent.
    """

    ALLOW_SELECT = True

    def __init__(self, *, id: str | None = None, show_match_markers: bool = True) -> None:
        super().__init__(id=id)
        self._strips: list[Strip] = []
        # Rendered base width, driving horizontal virtual_size.
        self._base_width: int = 0
        # In-development scrollbar match highlighting. False suppresses the
        # marker feed (the bar still scrolls; it just paints no markers).
        self._show_match_markers: bool = show_match_markers
        # Deferred scroll target — re-applied once there is a real viewport.
        self._pending_scroll_address: int | None = None
        self._pending_scroll_center: bool = False
        # Fraction of the viewport to drop a match below the top (context above
        # it); 0.0 = exact top. Carried with the pending scroll so a re-wrap /
        # resize re-applies the same margin.
        self._pending_scroll_context_fraction: float = 0.0

    # ── Substrate hooks ─────────────────────────────────────────

    def _visual_row(self, address: int) -> int:
        """Address -> visual row. Identity unless the substrate reflows."""
        return address

    def _ready_for_scroll(self) -> bool:
        return bool(self._strips)

    def _rebuild_for_width(self, width: int) -> bool:
        """Re-render for ``width``; return whether anything changed."""
        return False

    @property
    def match_rows(self) -> list[int]:
        """Sorted visual rows carrying a match — the scrollbar marker feed."""
        return []

    def row_of_chunk(self, chunk_id: int) -> int | None:
        return None

    def first_match_row_of_chunk(self, chunk_id: int) -> int | None:
        return None

    def _row_overlay_style(self, visual_y: int) -> Any:
        """Component-class style overlay for a row, or ``None``."""
        return None

    def top_address(self) -> int | None:
        """The address at the viewport top — a restorable reading position."""
        if not self._strips:
            return None
        return max(0, min(int(self.scroll_offset.y), len(self._strips) - 1))

    # ── Extent ──────────────────────────────────────────────────

    def _set_extent(self) -> None:
        self.virtual_size = Size(self._base_width, len(self._strips))

    @property
    def visual_line_count(self) -> int:
        """Number of visual rows in the cached buffer."""
        return len(self._strips)

    # ── Scrolling ───────────────────────────────────────────────

    def _scroll_target_y(self, visual_y: int, *, center: bool, context_fraction: float) -> int:
        """Visual-row scroll target: centred, dropped ``context_fraction`` down
        the viewport (so a match keeps context above it), or exact (top)."""
        if center:
            return max(0, visual_y - self.size.height // 2)
        if context_fraction > 0:
            return max(0, visual_y - int(self.size.height * context_fraction))
        return visual_y

    def scroll_to_address(
        self, address: int, *, center: bool = False, context_fraction: float = 0.0
    ) -> None:
        """Scroll so ``address`` is at the top (or centred, or dropped
        ``context_fraction`` down the viewport).

        Safe before layout: the target is remembered and re-applied once there
        is a real viewport.
        """
        self._pending_scroll_address = max(0, address)
        self._pending_scroll_center = center
        self._pending_scroll_context_fraction = context_fraction
        self._apply_pending_scroll()

    def _apply_pending_scroll(self, retries: int = 8) -> None:
        address = self._pending_scroll_address
        if address is None:
            return
        if self.size.height <= 0 or not self._ready_for_scroll():
            if retries > 0:
                self.call_after_refresh(self._apply_pending_scroll, retries - 1)
            return
        # Ensure the strips match the current viewport width before resolving
        # the address — otherwise an install at one width followed by layout at
        # another races the resize and scrolls to a stale row.
        if self.size.width > 0 and self._rebuild_for_width(self.size.width):
            self._refresh_match_scrollbar()
            self.refresh()
        target = self._scroll_target_y(
            self._visual_row(address),
            center=self._pending_scroll_center,
            context_fraction=self._pending_scroll_context_fraction,
        )
        self.scroll_to(y=target, animate=False, immediate=True)
        self._pending_scroll_address = None
        self._pending_scroll_center = False
        self._pending_scroll_context_fraction = 0.0

    def scroll_to_chunk(
        self,
        chunk_id: int,
        *,
        prefer_first_match: bool = True,
        center: bool = False,
        context_fraction: float = 0.0,
    ) -> None:
        """Scroll to a chunk by id. By default jumps to the chunk's first
        matched row if one exists; otherwise the chunk's first row."""
        target: int | None = None
        if prefer_first_match:
            target = self.first_match_row_of_chunk(chunk_id)
        if target is None:
            target = self.row_of_chunk(chunk_id)
        if target is not None:
            self.scroll_to_address(target, center=center, context_fraction=context_fraction)

    # ── Scrollbar markers ───────────────────────────────────────

    def _markers_enabled(self) -> bool:
        """Live read of the in-development toggle. Prefers the host app's
        ``_scrollbar_markers_enabled`` (so an in-menu toggle applies to a reused
        shared widget); falls back to the constructor value for test harnesses
        whose App isn't the FNDApp."""
        app_flag = getattr(self.app, "_scrollbar_markers_enabled", None)
        return self._show_match_markers if app_flag is None else bool(app_flag)

    def _refresh_match_scrollbar(self) -> None:
        bar = self.vertical_scrollbar
        if not isinstance(bar, MatchAwareScrollBar):
            return
        if not self._markers_enabled():
            # Clear stale markers so a live toggle-off takes effect.
            bar.set_match_lines([], self.visual_line_count)
            return
        bar.set_match_lines(self.match_rows, self.visual_line_count)

    @property
    def vertical_scrollbar(self) -> ScrollBar:
        """Use :class:`MatchAwareScrollBar` so match positions paint as accent
        markers on the track. Mirrors the override in
        :class:`fnd.tui.preview_scrollbar.MatchAwareScroll`.
        """
        if self._vertical_scrollbar is not None:
            return self._vertical_scrollbar
        scroll_bar = MatchAwareScrollBar(
            vertical=True,
            name="vertical",
            thickness=self.scrollbar_size_vertical,
        )
        self._vertical_scrollbar = scroll_bar
        scroll_bar.display = False
        self.app._start_widget(self, scroll_bar)
        return scroll_bar

    # ── ScrollView line API hook ────────────────────────────────

    def render_line(self, y: int) -> Strip:
        scroll_x, _ = self.scroll_offset
        scroll_y = int(self.scroll_offset.y)
        row = scroll_y + y
        base = self.rich_style
        if not (0 <= row < len(self._strips)):
            return Strip.blank(self.size.width, base)
        overlay = self._row_overlay_style(row)
        composed_base = base if overlay is None else (base + overlay)
        strip = self._strips[row].crop_extend(scroll_x, scroll_x + self.size.width, composed_base)
        # Composite every cell on ``composed_base`` so plain-text segments
        # (bgcolor=None straight out of Rich) paint at the widget's cascaded bg
        # — without this, those cells fall through to whatever the terminal's
        # default is (Tokyo Night terminal bg = ``#1a1b26``), producing a darker
        # stripe under the text while the padding shows ``$surface``.
        # ``composed_base + seg.style`` combines them in the right direction:
        # any explicit bg on the segment (e.g. match-highlight yellow)
        # overrides ``composed_base``.
        return Strip(
            Segment(seg.text, composed_base + (seg.style or Style()), seg.control) for seg in strip
        )

    # ── Resize hook ─────────────────────────────────────────────

    def on_resize(self, event: events.Resize) -> None:
        """Re-render on width change; re-apply pending scroll on height change."""
        if event.size.width > 0 and self._rebuild_for_width(event.size.width):
            self._refresh_match_scrollbar()
            self.refresh()
        if event.size.height > 0:
            self._apply_pending_scroll()

    # ── Selection (multi-line copy) ─────────────────────────────

    def get_selection(self, selection) -> tuple[str, str] | None:  # type: ignore[override]
        """Mirror ``Log.get_selection`` so Textual's clipboard manager can
        extract multi-line selections.

        ``selection`` carries ``(start, end)`` as ``(visual_row, col)`` pairs —
        Textual measures selection coordinates in visual rows (which is what
        ``render_line`` paints), not in logical lines. Each row's plain text
        comes from the cached Strip, so every substrate behaves identically.
        """
        if not self._strips:
            return None
        start = selection.start
        end = selection.end
        if start is None or end is None:
            return None
        sy, sx = start
        ey, ex = end
        if (sy, sx) > (ey, ex):
            sy, sx, ey, ex = ey, ex, sy, sx
        parts: list[str] = []
        for y in range(sy, ey + 1):
            if not (0 <= y < len(self._strips)):
                break
            plain = self._strips[y].text
            if sy == ey:
                parts.append(plain[sx:ex])
            elif y == sy:
                parts.append(plain[sx:])
            elif y == ey:
                parts.append(plain[:ex])
            else:
                parts.append(plain)
        if not parts:
            return None
        return "\n".join(parts), "\n"
