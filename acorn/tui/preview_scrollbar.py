"""Custom scrollbar that overlays match markers on the track.

Textual's stock ``ScrollBar`` paints a moving thumb over an otherwise
blank track; nothing on the bar itself tells the user where in the
document the query matches live. Per master plan §5 ("scroll bar should
mark match positions") and the user's repeated UX feedback, the marker
must live on the scrollbar — not as a sibling minimap.

This module wires three thin subclasses:

* :class:`MatchAwareScrollBarRender` — overrides Textual's renderer to
  swap the blank track glyph for an accent ``▌`` block at every row that
  maps to a chunk containing a query match.
* :class:`MatchAwareScrollBar` — passes a per-instance ``match_map`` into
  the custom renderer (Textual's default ``ScrollBar.render`` instantiates
  the renderer class without per-instance args).
* :class:`MatchAwareScroll` — a ``VerticalScroll`` whose vertical scrollbar
  is a :class:`MatchAwareScrollBar`. Exposes ``set_match_map`` so the TUI
  can update markers whenever the previewed file changes.

Marker rows are mapped from chunk index proportionally to bar height, so
a 30-chunk file painted on a 20-row bar still gives a sensible spread of
indicators.
"""

from __future__ import annotations

from math import ceil
from typing import TYPE_CHECKING, Any

from rich.color import Color as RichColor
from rich.segment import Segment, Segments
from rich.style import Style as RichStyle
from textual.containers import VerticalScroll
from textual.scrollbar import ScrollBar, ScrollBarRender

if TYPE_CHECKING:
    from rich.console import Console, ConsoleOptions, RenderResult

# Match markers use the same yellow accent as the inline body-text
# highlights so the scrollbar visually rhymes with the matched words.
_MARKER_COLOR = "#ffd866"
_MARKER_GLYPH = "▌"


class MatchAwareScrollBarRender(ScrollBarRender):
    """ScrollBarRender that paints accent markers on the track at every
    row whose proportionally-mapped chunk contains a query match."""

    def __init__(
        self,
        virtual_size: int = 100,
        window_size: int = 0,
        position: float = 0,
        thickness: int = 1,
        vertical: bool = True,
        style: Any = "bright_magenta on #555555",
        match_map: list[bool] | None = None,
    ) -> None:
        super().__init__(virtual_size, window_size, position, thickness, vertical, style)
        self.match_map: list[bool] = list(match_map or [])

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        size = (
            (options.height or console.height)
            if self.vertical
            else (options.max_width or console.width)
        )
        thickness = (
            (options.max_width or console.width)
            if self.vertical
            else (options.height or console.height)
        )
        _style = console.get_style(self.style)
        bar = self.render_bar(
            size=size,
            window_size=self.window_size,
            virtual_size=self.virtual_size,
            position=self.position,
            vertical=self.vertical,
            thickness=thickness,
            back_color=_style.bgcolor or RichColor.parse("#555555"),
            bar_color=_style.color or RichColor.parse("bright_magenta"),
        )

        if not (self.vertical and self.match_map and size > 0):
            yield bar
            return

        # Overlay markers on track cells. Thumb cells carry the
        # ``@mouse.down: grab`` meta — leave those untouched so the
        # current scroll position still reads as a thumb.
        segments = list(bar.segments)
        n = len(self.match_map)
        marker_color = RichColor.parse(_MARKER_COLOR)
        for i, seg in enumerate(segments):
            chunk_idx = min(int(i * n / size), n - 1)
            if not self.match_map[chunk_idx]:
                continue
            meta: dict[str, Any] = {}
            if seg.style is not None and seg.style.meta:
                meta = dict(seg.style.meta)
            if meta.get("@mouse.down") == "grab":
                continue
            bg = seg.style.bgcolor if seg.style is not None else None
            segments[i] = Segment(
                _MARKER_GLYPH,
                RichStyle(color=marker_color, bgcolor=bg, meta=meta),
            )
        yield Segments(segments, new_lines=True)


class MatchAwareScrollBar(ScrollBar):
    """ScrollBar that knows which chunks contain query matches.

    The default ``ScrollBar`` instantiates its renderer class without
    per-instance state, so we override ``_render_bar`` to pass ``match_map``
    through to :class:`MatchAwareScrollBarRender`.
    """

    def __init__(
        self,
        vertical: bool = True,
        name: str | None = None,
        *,
        thickness: int = 1,
    ) -> None:
        super().__init__(vertical=vertical, name=name, thickness=thickness)
        self._match_map: list[bool] = []

    def set_match_map(self, match_map: list[bool]) -> None:
        """Replace the chunk-match map and refresh the bar rendering."""
        new_map = list(match_map)
        if new_map == self._match_map:
            return
        self._match_map = new_map
        self.refresh()

    def _render_bar(self, scrollbar_style: Any) -> Any:
        window_size = self.window_size if self.window_size < self.window_virtual_size else 0
        virtual_size = self.window_virtual_size
        return MatchAwareScrollBarRender(
            virtual_size=ceil(virtual_size),
            window_size=ceil(window_size),
            position=self.position,
            thickness=self.thickness,
            vertical=self.vertical,
            style=scrollbar_style,
            match_map=self._match_map,
        )


class MatchAwareScroll(VerticalScroll):
    """VerticalScroll whose vertical scrollbar paints chunk-match markers.

    The vertical scrollbar is created lazily by the parent class via the
    ``vertical_scrollbar`` property; we override the property so the
    bar widget is a :class:`MatchAwareScrollBar` from first construction.
    """

    @property
    def vertical_scrollbar(self) -> ScrollBar:
        if self._vertical_scrollbar is not None:
            return self._vertical_scrollbar
        scroll_bar = MatchAwareScrollBar(
            vertical=True, name="vertical", thickness=self.scrollbar_size_vertical
        )
        self._vertical_scrollbar = scroll_bar
        scroll_bar.display = False
        self.app._start_widget(self, scroll_bar)
        return scroll_bar

    def set_match_map(self, match_map: list[bool]) -> None:
        """Forward the match map down to the custom scrollbar."""
        bar = self.vertical_scrollbar
        if isinstance(bar, MatchAwareScrollBar):
            bar.set_match_map(match_map)
