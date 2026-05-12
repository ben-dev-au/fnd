"""Flat virtualised line-buffer preview for plain-text formats.

Replaces the per-line ``Static`` widget tree the preview pane used for
PDF / TXT files. A 1002-page PDF previously inflated the Textual DOM to
~250,000 widgets (one ``Static`` per line, ~50 lines × ~5000 chunks),
which made the whole app lag on every keystroke because Textual's
layout and event-dispatch costs scale with the widget tree.

The line buffer collapses all of that to **one** widget. Its content
is a list of pre-rendered :class:`rich.text.Text` objects (one per
line), converted to :class:`textual.strip.Strip` once at build time
and stored. ``render_line`` returns the cached strip for the current
viewport row — Textual's line API only paints the visible viewport,
so the rendering cost is bounded by the terminal height (~50 rows),
not the document size.

Design notes:

* The user's per-line / per-chunk visual features survive: match
  highlights, focused-chunk band, and chunk-boundary gaps are baked
  into the Rich spans at build time. Match-scrollbar markers move to
  line-precise positions so they're accurate for files with mixed
  chunk sizes (the old chunk-uniform mapping was off for any chunk
  bigger or smaller than average).
* The widget is a thin ScrollView subclass — selection across lines
  becomes a single bounding-box operation (``ALLOW_SELECT = True``)
  so multi-line copy starts working too.
* Building a FileView is independent of mounting the widget. The
  helper :func:`build_file_view` is a pure function over decoded
  chunks; tests exercise it without the Textual pilot.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar

from rich.console import Console
from rich.segment import Segment
from rich.text import Text
from textual import events
from textual.geometry import Size
from textual.scroll_view import ScrollView
from textual.scrollbar import ScrollBar
from textual.strip import Strip

from acorn.tui.preview_scrollbar import MatchAwareScrollBar

if TYPE_CHECKING:
    pass


# Per-line accent overlays. Painted at ``render_line`` time via the
# widget's Textual component classes (which respect the active theme
# and resolve ``$accent N%`` to a concrete blended color). Rich's
# ``Style.parse`` rejects ``rgba(...)`` outright, so an earlier
# attempt to bake the overlays into the Strips via Rich styles
# produced no visible highlight at all — the component-class route is
# the right idiom.
_COMPONENT_MATCH_LINE = "line-buffer--match"
_COMPONENT_FOCUSED_CHUNK = "line-buffer--focused-chunk"


@dataclass(slots=True)
class FileView:
    """Flat, immutable rendering of a file's chunks as a line buffer.

    ``lines`` is the sole source of truth for what gets painted; the
    other fields are indexes that map UI events (clicking a section
    in the sidebar, focusing a chunk) onto line offsets in ``lines``.
    """

    # One Rich Text per visible row. Chunk-boundary gap rows are stored
    # as ``Text("")`` so a single ``y -> lines[y]`` lookup serves every
    # render_line call.
    lines: list[Text] = field(default_factory=list)
    # Parallel to ``lines``: which chunk owns this row. Gap rows carry
    # the index of the chunk they precede so a click-through to the
    # gap still resolves to a real chunk.
    line_to_chunk: list[int] = field(default_factory=list)
    # chunk_id -> (start_line, end_line_exclusive) covering both the
    # gap row (if any) and the chunk's text lines.
    chunk_to_range: dict[int, tuple[int, int]] = field(default_factory=dict)
    # chunk_id -> the first line index in that chunk that contains a
    # match. Drives "scroll to the matched line, not just the chunk
    # top" when the sidebar resolves a section click.
    first_hit_line_in_chunk: dict[int, int] = field(default_factory=dict)
    # All line indices that contain at least one match. Drives the
    # scrollbar marker map directly — every entry is a line position,
    # so marker accuracy is bounded by terminal-cell resolution, not
    # chunk granularity.
    match_lines: set[int] = field(default_factory=set)

    @property
    def line_count(self) -> int:
        return len(self.lines)

    @property
    def widest_line(self) -> int:
        return max((line.cell_len for line in self.lines), default=0)


def build_file_view(
    chunks: list[tuple[int, str, list[tuple[int, int]]]],
    *,
    insert_chunk_gaps: bool = True,
) -> FileView:
    """Construct a FileView from decoded chunk data.

    ``chunks`` is a list of ``(chunk_id, chunk_text, match_spans)``:
      * ``chunk_id``: stable identifier from the search index.
      * ``chunk_text``: the decoded chunk body. Splits on ``\\n``.
      * ``match_spans``: ``(byte_start, byte_end)`` ranges within the
        chunk text that contain a query match. Each range gets its
        line(s) tagged in ``match_lines`` and the substring styled
        bold.

    ``insert_chunk_gaps=True`` adds a blank row between consecutive
    chunks so the user can see where chunks split. The gap row belongs
    to the *following* chunk for offset-mapping purposes.

    Pure function — no widget mounting, no Textual side-effects. Used
    by the production preview pipeline AND by tests that want to
    assert on the resulting FileView shape.
    """
    fv = FileView()
    for nth, (chunk_id, chunk_text, match_spans) in enumerate(chunks):
        # Compute which lines within the chunk contain a match.
        text = chunk_text.replace("\r\n", "\n")
        # Find each line's [start, end) byte offset within the chunk.
        line_offsets: list[tuple[int, int, str]] = []
        cursor = 0
        for raw_line in text.split("\n"):
            line_offsets.append((cursor, cursor + len(raw_line), raw_line))
            cursor += len(raw_line) + 1  # +1 for the dropped \n
        chunk_first_match: int | None = None
        local_match_offsets: list[set[tuple[int, int]]] = [set() for _ in line_offsets]
        for span_start, span_end in match_spans:
            for li, (lstart, lend, _) in enumerate(line_offsets):
                if span_end <= lstart or span_start >= lend:
                    continue
                clipped = (max(0, span_start - lstart), min(lend - lstart, span_end - lstart))
                local_match_offsets[li].add(clipped)
        chunk_start = len(fv.lines)
        # Chunk-boundary gap row (skipped for the very first chunk).
        if insert_chunk_gaps and nth > 0:
            fv.lines.append(Text(""))
            fv.line_to_chunk.append(chunk_id)
        for (_, _, raw_line), span_set in zip(line_offsets, local_match_offsets, strict=False):
            global_line_idx = len(fv.lines)
            t = Text(raw_line)
            if span_set:
                # Per-match bold span. Row-level background tint is
                # applied at render time via the widget's component
                # class (see :class:`LineBufferPreview`), not baked in
                # here — Rich rejects ``rgba(...)`` and the legacy
                # ``$accent N%`` form is only meaningful inside
                # Textual CSS.
                for span_start, span_end in span_set:
                    t.stylize("bold", span_start, span_end)
                fv.match_lines.add(global_line_idx)
                if chunk_first_match is None:
                    chunk_first_match = global_line_idx
            fv.lines.append(t)
            fv.line_to_chunk.append(chunk_id)
        chunk_end = len(fv.lines)
        fv.chunk_to_range[chunk_id] = (chunk_start, chunk_end)
        if chunk_first_match is not None:
            fv.first_hit_line_in_chunk[chunk_id] = chunk_first_match
    return fv


class LineBufferPreview(ScrollView, can_focus=True):
    """Flat-buffer preview widget for plain-text formats.

    Public surface (the only methods the host app should call):

    * :meth:`set_file_view` — install a fresh FileView. Recomputes the
      virtual size and the cached Strips. Cheap to call when the query
      changes, since the cost is O(lines) and there's no DOM churn.
    * :meth:`scroll_to_line` — jump to a specific line index, with an
      optional ``center=True`` to put the line mid-viewport rather
      than at the top.
    * :meth:`scroll_to_chunk` — convenience: scroll to a chunk by id,
      optionally to the chunk's first matched line.
    * :meth:`set_focused_chunk` — paint a stronger accent band over
      the focused chunk's lines. Pass ``None`` to clear.
    * :attr:`match_lines` — the sorted set of line indices the
      scrollbar renderer maps for feature 6 accuracy.
    """

    ALLOW_SELECT = True

    COMPONENT_CLASSES: ClassVar[set[str]] = {
        _COMPONENT_MATCH_LINE,
        _COMPONENT_FOCUSED_CHUNK,
    }

    DEFAULT_CSS = """
    LineBufferPreview {
        background: $surface;
        color: $text;
        width: 1fr;
        height: 1fr;
        scrollbar-gutter: stable;
    }
    LineBufferPreview.-hidden { display: none; }
    LineBufferPreview > .line-buffer--match {
        background: $accent 8%;
    }
    LineBufferPreview > .line-buffer--focused-chunk {
        background: $accent 15%;
    }
    """

    def __init__(self, *, id: str | None = None, wrap: bool = False) -> None:
        super().__init__(id=id)
        self._fv: FileView | None = None
        # Visual-row Strips actually painted by ``render_line``. With
        # wrap disabled this is parallel to ``fv.lines``; with wrap
        # enabled one logical line can produce several visual rows.
        self._strips: list[Strip] = []
        # Parallel to ``_strips``: which FileView line this visual row
        # belongs to. Lets ``render_line`` paint, the focused-chunk
        # overlay locate its slice, and the match-line property project
        # logical hits onto visual cells for the scrollbar.
        self._visual_to_logical: list[int] = []
        # Parallel to ``fv.lines``: the visual row where each logical
        # line begins. Used by ``scroll_to_line`` / ``scroll_to_chunk``
        # to translate sidebar-supplied logical indices into viewport
        # offsets after wrap.
        self._logical_to_visual_start: list[int] = []
        self._focused_chunk: int | None = None
        # Rendered base width — set from FileView.widest_line at build
        # time so horizontal scrolling reports correct virtual_size.
        self._base_width: int = 0
        # When ``True``, ``set_file_view`` and ``on_resize`` re-wrap the
        # cached Strips to viewport width. The PDF / TXT pipeline turns
        # this on so long tabular lines stay readable without horizontal
        # scrolling. False (default) preserves the existing test-fixture
        # behaviour: lines render verbatim, overflow goes to horizontal
        # scroll.
        self._wrap: bool = wrap
        # Width the cached Strips were wrapped to. ``0`` = unwrapped.
        # Tracked separately from ``self.size.width`` so ``on_resize``
        # can short-circuit when the width didn't actually change.
        self._wrap_width: int = 0

    # ── Public API ──────────────────────────────────────────────

    def set_file_view(self, fv: FileView) -> None:
        """Install ``fv``, rebuild cached Strips, reset scroll.

        Also pushes the line-precise match positions onto the widget's
        own scrollbar (a :class:`MatchAwareScrollBar`) so the bar
        markers update in lockstep with the buffer's content — the
        host never has to wire scrollbar data through separately.

        Safe to call before the widget has been laid out: when wrap is
        enabled but ``size.width`` is still 0 (typical immediately
        after ``mount``) the first rebuild produces unwrapped strips
        and a deferred ``call_after_refresh`` re-runs the rebuild once
        the layout pass has assigned a real width.
        """
        self._fv = fv
        self._focused_chunk = None
        self._rebuild_strips()
        self.scroll_to(0, 0, animate=False, immediate=True)
        self._refresh_match_scrollbar()
        self.refresh()
        if self._wrap and self.size.width == 0:
            # Schedule a post-layout rebuild so wrap actually fires on
            # the first paint after mount.
            self.call_after_refresh(self._rebuild_after_layout)

    def _rebuild_after_layout(self) -> None:
        if self._fv is None:
            return
        if self._wrap and self.size.width > 0 and self.size.width != self._wrap_width:
            self._rebuild_strips()
            self._refresh_match_scrollbar()
            self.refresh()

    def _refresh_match_scrollbar(self) -> None:
        bar = self.vertical_scrollbar
        if isinstance(bar, MatchAwareScrollBar):
            bar.set_match_lines(self.match_lines, self.visual_line_count)

    def clear(self) -> None:
        """Empty the buffer."""
        self.set_file_view(FileView())

    def set_wrap(self, enabled: bool) -> None:
        """Toggle viewport-width line wrapping. Rebuilds cached Strips
        when the mode changes so the next paint reflects the new layout
        immediately (the alternative — wrapping lazily inside
        ``render_line`` — would re-wrap on every keystroke)."""
        if self._wrap == enabled:
            return
        self._wrap = enabled
        if self._fv is not None:
            self._rebuild_strips()
            self.refresh()

    def scroll_to_line(self, line_index: int, *, center: bool = False) -> None:
        """Move the viewport so the logical line at ``line_index`` is visible.

        ``center=False`` (default) puts the line at the top of the
        viewport — matches the legacy ``scroll_to_widget(top=True)``
        behaviour. ``center=True`` vertically centres the line so the
        user has context above and below.

        With wrap enabled the logical index is translated to its first
        visual row before scrolling — the user still sees the wrapped
        line top-of-viewport, not whichever visual row happened to
        carry the match span.
        """
        if self._fv is None or self._fv.line_count == 0:
            return
        line_index = max(0, min(line_index, self._fv.line_count - 1))
        visual_y = self._logical_to_visual_y(line_index)
        target = max(0, visual_y - self.size.height // 2) if center else visual_y
        self.scroll_to(y=target, animate=False, immediate=True)

    def scroll_to_chunk(
        self,
        chunk_id: int,
        *,
        prefer_first_match: bool = True,
        center: bool = False,
    ) -> None:
        """Scroll to a chunk by id. By default jumps to the chunk's
        first matched line if one exists; otherwise the chunk's first
        line."""
        if self._fv is None:
            return
        target: int | None = None
        if prefer_first_match:
            target = self._fv.first_hit_line_in_chunk.get(chunk_id)
        if target is None:
            rng = self._fv.chunk_to_range.get(chunk_id)
            if rng is not None:
                target = rng[0]
        if target is not None:
            self.scroll_to_line(target, center=center)

    def set_focused_chunk(self, chunk_id: int | None) -> None:
        """Paint the focused-chunk accent band over ``chunk_id`` and
        clear it from the previously focused one.

        Cheap — just flips the focused id and asks the widget to repaint.
        The actual overlay style is applied in :meth:`render_line` via
        a component class, so no Strip rebuild is needed.
        """
        if self._focused_chunk == chunk_id or self._fv is None:
            return
        self._focused_chunk = chunk_id
        self.refresh()

    @property
    def match_lines(self) -> list[int]:
        """Sorted **visual** row indices containing matches.

        With wrap disabled each logical line is one visual row, so this
        is identical to ``sorted(fv.match_lines)``. With wrap enabled
        each match's logical line is mapped to its first visual row —
        which is the row the scrollbar marker should point at so the
        thumb lands on a row that visibly contains the match.
        """
        if self._fv is None:
            return []
        return sorted({self._logical_to_visual_y(li) for li in self._fv.match_lines})

    @property
    def visual_line_count(self) -> int:
        """Number of visual rows in the cached buffer. Equals
        ``fv.line_count`` when wrap is off; ≥ ``fv.line_count`` when
        wrap is on. The scrollbar's ``total_lines`` should use this
        value (not ``fv.line_count``) so markers project onto the
        actual visual track."""
        return len(self._strips)

    @property
    def file_view(self) -> FileView | None:
        return self._fv

    # ── ScrollView line API hook ────────────────────────────────

    def render_line(self, y: int) -> Strip:
        scroll_x, _ = self.scroll_offset
        scroll_y = int(self.scroll_offset.y)
        line_idx = scroll_y + y
        if not (0 <= line_idx < len(self._strips)):
            return Strip.blank(self.size.width, self.rich_style)
        # Resolve the per-row accent overlay (focused chunk wins over
        # match line; both compose with the strip's own foreground
        # styles via Rich's standard background-overlay semantics).
        overlay = self._row_overlay_style(line_idx)
        fill_style = overlay if overlay is not None else self.rich_style
        strip = self._strips[line_idx].crop_extend(scroll_x, scroll_x + self.size.width, fill_style)
        if overlay is not None:
            strip = strip.apply_style(overlay)
        return strip

    def _row_overlay_style(self, visual_y: int) -> Any:
        """Return the component-class style overlay for this visual row,
        or ``None`` if the row doesn't need an accent. The focused-chunk
        overlay takes precedence; otherwise match lines get the lighter
        tint."""
        if self._fv is None:
            return None
        if visual_y >= len(self._visual_to_logical):
            return None
        logical = self._visual_to_logical[visual_y]
        focused = self._focused_chunk
        if focused is not None:
            rng = self._fv.chunk_to_range.get(focused)
            if rng is not None and rng[0] <= logical < rng[1]:
                return self.get_component_rich_style(_COMPONENT_FOCUSED_CHUNK)
        if logical in self._fv.match_lines:
            return self.get_component_rich_style(_COMPONENT_MATCH_LINE)
        return None

    # ── Scrollbar override ──────────────────────────────────────

    @property
    def vertical_scrollbar(self) -> ScrollBar:
        """Use :class:`MatchAwareScrollBar` so the buffer's match
        positions paint as accent markers on the track. Mirrors the
        override in :class:`acorn.tui.preview_scrollbar.MatchAwareScroll`.
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

    # ── Resize hook ─────────────────────────────────────────────

    def on_resize(self, event: events.Resize) -> None:
        """Re-wrap to viewport width when wrap is enabled and the width
        actually changed. Skips the rebuild in the (very common) case
        where only the height moved."""
        if (
            self._wrap
            and self._fv is not None
            and event.size.width != self._wrap_width
            and event.size.width > 0
        ):
            self._rebuild_strips()
            self._refresh_match_scrollbar()
            self.refresh()

    # ── Rendering helpers ───────────────────────────────────────

    def _logical_to_visual_y(self, logical_index: int) -> int:
        """Translate a logical line index to its first visual row.

        Out-of-range indices clamp; an empty buffer returns 0 so
        callers don't need defensive checks of their own.
        """
        if not self._logical_to_visual_start:
            return 0
        if logical_index <= 0:
            return self._logical_to_visual_start[0]
        if logical_index >= len(self._logical_to_visual_start):
            return self._logical_to_visual_start[-1]
        return self._logical_to_visual_start[logical_index]

    def _rebuild_strips(self) -> None:
        """Render every logical line to one or more cached Strips.

        Owns the bookkeeping for both wrap modes: populates
        ``_strips``, ``_visual_to_logical``, and
        ``_logical_to_visual_start`` in one pass; resets ``virtual_size``
        so the scrollbar sizes itself correctly for the new visual
        count. Per-row accent overlays (match line, focused chunk) are
        applied at render time in :meth:`render_line` via component
        classes — keeping the strips style-neutral here means a focus
        change doesn't require a full rebuild.
        """
        fv = self._fv
        if fv is None:
            self._strips = []
            self._visual_to_logical = []
            self._logical_to_visual_start = []
            self._base_width = 0
            self.virtual_size = Size(0, 0)
            return

        # Wrap width: 0 = no wrap (legacy behaviour, used by tests).
        # > 0 = wrap to that width; tracks the viewport size.
        wrap_width = self.size.width if self._wrap and self.size.width > 0 else 0
        self._wrap_width = wrap_width

        strips, v2l, l2vs = self._render_lines(fv.lines, wrap_width=wrap_width)
        self._strips = strips
        self._visual_to_logical = v2l
        self._logical_to_visual_start = l2vs
        if wrap_width > 0:
            # Wrapping forces every visual row to fit inside the viewport
            # — horizontal scroll is meaningless, so report a width of 1
            # (Textual clamps to viewport on its own from there).
            self._base_width = max(1, wrap_width)
        else:
            self._base_width = max(fv.widest_line, 1)
        self.virtual_size = Size(self._base_width, len(strips))

    @staticmethod
    def _render_lines(
        lines: list[Text],
        *,
        wrap_width: int,
    ) -> tuple[list[Strip], list[int], list[int]]:
        """Render ``lines`` to Strips, splitting at ``\\n`` segments.

        ``wrap_width`` of 0 disables wrapping (single visual row per
        logical line). A positive value wraps via Rich's overflow=fold
        path, producing one Strip per visual row and recording the
        logical-to-visual mapping.

        Returns ``(strips, visual_to_logical, logical_to_visual_start)``.
        ``logical_to_visual_start`` is parallel to ``lines`` (one entry
        per logical line); empty logical lines still consume one
        visual row so ``v2l`` and ``l2vs`` always agree.
        """
        if wrap_width > 0:
            console = Console(width=wrap_width, file=None, force_terminal=False)
            opts = console.options.update(max_width=wrap_width, overflow="fold", no_wrap=False)
        else:
            console = Console(width=10_000, file=None, force_terminal=False)
            opts = console.options.update(max_width=10_000, overflow="ignore", no_wrap=True)

        strips: list[Strip] = []
        v2l: list[int] = []
        l2vs: list[int] = [0] * len(lines)
        for li, line in enumerate(lines):
            l2vs[li] = len(strips)
            current: list[Segment] = []
            produced_any = False
            for seg in console.render(line, opts):
                if seg.control:
                    continue
                if seg.text == "\n":
                    strips.append(Strip(current))
                    v2l.append(li)
                    current = []
                    produced_any = True
                    continue
                if "\n" not in seg.text:
                    current.append(seg)
                    continue
                # ``overflow=fold`` packs the entire wrapped block into
                # a single segment with embedded ``\n`` separators.
                # Split here so each visual row gets its own Strip.
                parts = seg.text.split("\n")
                for j, part in enumerate(parts):
                    if j > 0:
                        strips.append(Strip(current))
                        v2l.append(li)
                        current = []
                        produced_any = True
                    if part:
                        current.append(Segment(part, seg.style))
            if current:
                strips.append(Strip(current))
                v2l.append(li)
                produced_any = True
            if not produced_any:
                # Empty logical line (chunk-boundary gap or trailing
                # blank). Emit one empty visual row so v2l and l2vs
                # stay parallel; otherwise scroll_to_line would land
                # on the wrong row for any logical index past the gap.
                strips.append(Strip([]))
                v2l.append(li)
        return strips, v2l, l2vs

    # ── Selection (multi-line copy) ─────────────────────────────

    def get_selection(self, selection) -> tuple[str, str] | None:  # type: ignore[override]
        """Mirror ``Log.get_selection`` so Textual's clipboard manager
        can extract multi-line selections from the buffer.

        ``selection`` carries ``(start, end)`` as ``(visual_row, col)``
        pairs — Textual measures selection coordinates in visual rows
        (which is what ``render_line`` paints), not in logical lines.
        We read each visual row's plain text from the cached Strip so
        wrap mode and no-wrap mode behave identically here.
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


def match_marker_positions(match_lines: list[int], track_height: int, total_lines: int) -> set[int]:
    """Compute which scrollbar-track cells should carry a match marker.

    Replaces ``MatchAwareScrollBar``'s chunk-uniform mapping with a
    line-precise one: every match line is projected to a cell index
    via ``cell = int(line * track_height / total_lines)``, so a single
    big chunk and many tiny ones both place markers at the right
    visual position on the track.

    Returned as a set so the renderer can do an O(1) "is this cell a
    marker?" check while painting.
    """
    if track_height <= 0 or total_lines <= 0:
        return set()
    return {
        min(int(line * track_height / total_lines), track_height - 1)
        for line in match_lines
        if 0 <= line < total_lines
    }


__all__ = [
    "FileView",
    "LineBufferPreview",
    "build_file_view",
    "match_marker_positions",
]
