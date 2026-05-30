"""Custom scrollbar that overlays match markers on the track.

Textual's stock ``ScrollBar`` paints a moving thumb over an otherwise
blank track; nothing on the bar itself tells the user where in the
document the query matches live. Per master plan §5 ("scroll bar should
mark match positions") and the user's repeated UX feedback, the marker
must live on the scrollbar — not as a sibling minimap.

This module wires three thin subclasses:

* :class:`MatchAwareScrollBarRender` — overrides Textual's renderer to
  swap the blank track glyph for an accent ``▌`` block at every row that
  carries a match.
* :class:`MatchAwareScrollBar` — passes per-instance match data into the
  custom renderer (Textual's default ``ScrollBar.render`` instantiates the
  renderer class without per-instance args).
* :class:`MatchAwareScroll` — a ``VerticalScroll`` whose vertical scrollbar
  is a :class:`MatchAwareScrollBar`.

Two marker-mapping modes coexist:

* **Line-precise** (preferred, Phase 3 redesign) — driven by
  ``set_match_lines(lines, total_lines)``. Each match line maps to one
  exact track cell via ``cell = int(line * track_height / total_lines)``,
  so a single big chunk and many tiny ones each get a marker at the right
  visual position. Used by the flat-buffer :class:`LineBufferPreview`
  pipeline.
* **Chunk-uniform** (legacy) — driven by ``set_match_map(match_map)``
  where ``match_map[i]`` flags chunk ``i`` as match-bearing. Each cell on
  the track resolves back to a chunk index proportionally and paints if
  that chunk matches. Used by the structural Markdown / docx / pptx
  preview path until Phase 5 migrates it.
"""

from __future__ import annotations

import contextlib
from math import ceil
from typing import TYPE_CHECKING, Any

from rich.color import Color as RichColor
from rich.segment import Segment, Segments
from rich.style import Style as RichStyle
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.scrollbar import ScrollBar, ScrollBarRender

if TYPE_CHECKING:
    from rich.console import Console, ConsoleOptions, RenderResult

# Match markers use the same yellow accent as the inline body-text
# highlights so the scrollbar visually rhymes with the matched words.
_MARKER_COLOR = "#ffd866"
_MARKER_GLYPH = "▌"

# The thumb is one box-drawing vertical per cell — the same line weight as
# the pane's ``round`` border, so the bar reads as part of the frame rather
# than a fat block. Gappy on Apple Terminal / SF Mono (font-determined, same
# class as the box-border issue) — a documented "use a modern terminal" case.
_THUMB_GLYPH = "│"


class ThinScrollBarRender(ScrollBarRender):
    """Renderer for a thin, constant-size scrollbar thumb.

    Two departures from the stock renderer, installed app-wide via
    ``ScrollBar.renderer`` so every Textual scrollbar (results/sidebar trees,
    code fences, settings lists) matches:

    * The thumb is a single box-drawing vertical (``│``) per cell in the bar
      colour over a transparent track — the pane border's line weight, not a
      reverse-video full-cell block.
    * The thumb is a **constant** integer number of cells. The stock renderer
      packs sub-cell precision into partial-block end caps, so its cell count
      flickers ±1 as you scroll (the thumb visibly resizes). Here the count is
      derived once from the window/content ratio and only the position moves.

    Vertical bars only; horizontal bars fall through to the stock renderer
    (rare, and line wrapping removes the code-fence ones).
    """

    def _thin_segments(
        self, size: int, bar_color: RichColor, back_color: RichColor | None
    ) -> list[Segment]:
        # ``back_color`` may be None (transparent track) — don't fabricate a bg.
        track = RichStyle(bgcolor=back_color)
        if size <= 0:
            return []
        window = self.window_size
        virtual = self.virtual_size
        if not (window and virtual and virtual != size):
            # Whole content visible (or degenerate): blank track, no thumb.
            return [Segment(" ", track) for _ in range(size)]
        # Constant thumb height from the window/content ratio (≥1 cell). Depends
        # only on window/virtual/size — not position — so scrolling never resizes
        # it; only ``top`` moves.
        thumb = max(1, min(size, round(window * size / virtual)))
        max_top = size - thumb
        denom = virtual - window
        ratio = (self.position / denom) if denom > 0 else 0.0
        ratio = 0.0 if ratio < 0 else (1.0 if ratio > 1 else ratio)
        top = round(max_top * ratio)
        top = 0 if top < 0 else (max_top if top > max_top else top)
        thumb_style = RichStyle(color=bar_color, meta={"@mouse.down": "grab"})
        up = RichStyle(bgcolor=back_color, meta={"@mouse.down": "scroll_up"})
        down = RichStyle(bgcolor=back_color, meta={"@mouse.down": "scroll_down"})
        return [
            Segment(_THUMB_GLYPH, thumb_style)
            if top <= i < top + thumb
            else Segment(" ", up if i < top else down)
            for i in range(size)
        ]

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        if not self.vertical:
            yield from super().__rich_console__(console, options)
            return
        size = options.height or console.height
        style = console.get_style(self.style)
        bar_color = style.color or RichColor.parse("bright_magenta")
        yield Segments(self._thin_segments(size, bar_color, style.bgcolor), new_lines=True)


class MatchAwareScrollBarRender(ThinScrollBarRender):
    """ScrollBarRender that paints accent markers on the track.

    Accepts either of two marker sources (mutually exclusive):

    * Line-precise (Phase 3): ``match_lines`` + ``total_lines``. Each
      match line maps to one exact track cell.
    * Chunk-uniform (legacy): ``match_map[i]`` flagging chunk ``i``;
      track cells proportionally resolve back to chunk indices.

    Passing both is supported but line-precise wins (and ``match_map``
    is ignored) since it is strictly more accurate.
    """

    def __init__(
        self,
        virtual_size: int = 100,
        window_size: int = 0,
        position: float = 0,
        thickness: int = 1,
        vertical: bool = True,
        style: Any = "bright_magenta on #555555",
        match_map: list[bool] | None = None,
        match_lines: list[int] | None = None,
        total_lines: int = 0,
    ) -> None:
        super().__init__(virtual_size, window_size, position, thickness, vertical, style)
        self.match_map: list[bool] = list(match_map or [])
        self.match_lines: list[int] = list(match_lines or [])
        self.total_lines: int = max(0, int(total_lines))

    def _marker_cells(self, size: int) -> set[int]:
        """Resolve the set of track-cell indices that should carry a marker.

        Prefers the line-precise mapping when ``match_lines`` /
        ``total_lines`` are populated; falls back to the chunk-uniform
        mapping driven by ``match_map``. Returns an empty set when
        neither source is available.
        """
        if size <= 0:
            return set()
        if self.match_lines and self.total_lines > 0:
            return {
                min(int(line * size / self.total_lines), size - 1)
                for line in self.match_lines
                if 0 <= line < self.total_lines
            }
        if not self.match_map:
            return set()
        n = len(self.match_map)
        return {i for i in range(size) if self.match_map[min(int(i * n / size), n - 1)]}

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        # Build the thin, constant-size bar (vertical only), then overlay
        # match markers on the surviving track cells.
        if not self.vertical:
            yield from super().__rich_console__(console, options)
            return
        size = options.height or console.height
        style = console.get_style(self.style)
        bar_color = style.color or RichColor.parse("bright_magenta")
        segments = self._thin_segments(size, bar_color, style.bgcolor)

        marker_cells = self._marker_cells(size)
        if marker_cells:
            # Thumb cells carry the ``@mouse.down: grab`` meta — leave those
            # untouched so the current scroll position still reads as a thumb.
            marker_color = RichColor.parse(_MARKER_COLOR)
            for i, seg in enumerate(segments):
                if i not in marker_cells:
                    continue
                meta: dict[str, Any] = (
                    dict(seg.style.meta) if (seg.style is not None and seg.style.meta) else {}
                )
                if meta.get("@mouse.down") == "grab":
                    continue
                bg = seg.style.bgcolor if seg.style is not None else None
                segments[i] = Segment(
                    _MARKER_GLYPH,
                    RichStyle(color=marker_color, bgcolor=bg, meta=meta),
                )
        yield Segments(segments, new_lines=True)


class MatchAwareScrollBar(ScrollBar):
    """ScrollBar that knows where in the document the query matched.

    Accepts marker data from either the line-precise pipeline
    (``set_match_lines``, used by the flat-buffer preview) or the
    chunk-uniform legacy pipeline (``set_match_map``, used by the
    structural Markdown preview). When both are set the line-precise
    data wins inside the renderer.

    The default ``ScrollBar`` instantiates its renderer class without
    per-instance state, so we override ``_render_bar`` to pass the
    match data through to :class:`MatchAwareScrollBarRender`.
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
        self._match_lines: list[int] = []
        self._total_lines: int = 0

    def set_match_map(self, match_map: list[bool]) -> None:
        """Replace the chunk-match map and refresh the bar rendering.

        Legacy chunk-uniform path retained for the structural preview;
        new callers should prefer :meth:`set_match_lines` which produces
        cell-accurate markers regardless of chunk size variance.
        """
        new_map = list(match_map)
        if new_map == self._match_map and not self._match_lines:
            return
        self._match_map = new_map
        # A caller that sets a chunk match-map is explicitly NOT using the
        # line-precise pipeline; clear any stale line data so the renderer
        # picks the chunk-uniform code path.
        self._match_lines = []
        self._total_lines = 0
        self.refresh()

    def set_match_lines(self, match_lines: list[int], total_lines: int) -> None:
        """Replace the line-precise marker positions and refresh the bar.

        ``match_lines`` is the sorted list of line indices that contain a
        match; ``total_lines`` is the file's full line count. Each entry
        maps to one exact track cell at render time.
        """
        new_lines = list(match_lines)
        new_total = max(0, int(total_lines))
        if (
            new_lines == self._match_lines
            and new_total == self._total_lines
            and not self._match_map
        ):
            return
        self._match_lines = new_lines
        self._total_lines = new_total
        # Same reciprocity as set_match_map: when the line-precise data
        # is supplied, drop any leftover chunk map so the renderer's
        # selection between modes is unambiguous.
        self._match_map = []
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
            match_lines=self._match_lines,
            total_lines=self._total_lines,
        )


class MatchAwareScroll(VerticalScroll):
    """VerticalScroll whose vertical scrollbar paints chunk-match markers.

    The vertical scrollbar is created lazily by the parent class via the
    ``vertical_scrollbar`` property; we override the property so the
    bar widget is a :class:`MatchAwareScrollBar` from first construction.
    """

    # Bridge focus back to the results tree when the user is already
    # parked at scroll_x = 0 (the preview wraps to width, so horizontal
    # scroll is essentially always 0 in practice). Overrides the parent
    # ``scroll_left`` binding so a single Left exits the pane instead of
    # paging horizontally into empty space.
    BINDINGS = [  # noqa: RUF012 — Textual widget BINDINGS expects a class-level list
        Binding("left", "bridge_left", "Focus results", show=False),
    ]

    def watch_scroll_y(self, old_value: float, new_value: float) -> None:
        # Bubble to the app so it can extend the mounted chunk window
        # when the viewport approaches a boundary. The app debounces
        # internally so consecutive watcher trips (e.g., from a
        # programmatic scroll-to-widget) collapse to a single check.
        # Missing handler is a silent no-op so this widget stays
        # usable in isolation.
        super().watch_scroll_y(old_value, new_value)
        try:
            handler = getattr(self.app, "_schedule_preview_lazy_mount_check", None)
            if handler is not None:
                # Pass focus so the app can tell a user scroll (pane focused,
                # e.g. Reading View) from a programmatic one (navigation /
                # container swap, while the results tree holds focus).
                handler(user_initiated=self.has_focus)
        except Exception:
            pass

    def watch_has_focus(self, has_focus: bool) -> None:
        """Skip Textual's default behaviour of reapplying CSS to every
        descendant when this widget gains or loses focus.

        The stock ``Widget.watch_has_focus`` calls
        ``self.update_node_styles()``, which walks the whole subtree and
        reapplies CSS to every node (every chunk widget, MarkdownBlock,
        flat-buffer line). That walk is the dominant cost when the user
        Tab-cycles in or out of the preview pane on a large document.

        None of this pane's descendants have CSS rules that depend on
        the pane's ``:focus`` state, so we apply styles to just this
        node — the focus-indicator border on the pane itself.
        """
        with contextlib.suppress(Exception):
            self.app.stylesheet.apply(self)

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
        """Forward a chunk-uniform match map down to the custom scrollbar.

        Legacy path used by the structural Markdown preview; flat-buffer
        callers should use :meth:`set_match_lines` instead.
        """
        bar = self.vertical_scrollbar
        if isinstance(bar, MatchAwareScrollBar):
            bar.set_match_map(match_map)

    def set_match_lines(self, match_lines: list[int], total_lines: int) -> None:
        """Forward line-precise marker positions down to the custom scrollbar."""
        bar = self.vertical_scrollbar
        if isinstance(bar, MatchAwareScrollBar):
            bar.set_match_lines(match_lines, total_lines)

    def action_bridge_left(self) -> None:
        """Left-arrow: hand focus to the results tree, or fall back to
        horizontal scroll when the pane actually has somewhere to go."""
        if self.scroll_x > 0:
            self.scroll_left()
            return
        try:
            results = self.app.query_one("#results_pane")
        except Exception:
            return
        results.focus()
