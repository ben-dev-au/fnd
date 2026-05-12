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
from typing import TYPE_CHECKING

from rich.console import Console
from rich.segment import Segment
from rich.text import Text
from textual.geometry import Size
from textual.scroll_view import ScrollView
from textual.strip import Strip

if TYPE_CHECKING:
    pass


# Per-line accent overlays. The visual intent matches the legacy CSS
# classes (chunk-line-match @ 8% and chunk-section-focused @ 15%) but
# rendered through Rich style strings so the line buffer can paint
# them without leaning on Textual's per-widget CSS engine.
_MATCH_LINE_STYLE = "on rgba(255,180,90,0.10)"  # ~ accent 8%
_FOCUSED_CHUNK_STYLE = "on rgba(255,180,90,0.18)"  # ~ accent 15%


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
                t.stylize(_MATCH_LINE_STYLE)
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

    DEFAULT_CSS = """
    LineBufferPreview {
        background: $surface;
        color: $text;
        scrollbar-gutter: stable;
    }
    """

    def __init__(self, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self._fv: FileView | None = None
        self._strips: list[Strip] = []
        self._focused_chunk: int | None = None
        # Rendered base width — set from FileView.widest_line at build
        # time so horizontal scrolling reports correct virtual_size.
        self._base_width: int = 0

    # ── Public API ──────────────────────────────────────────────

    def set_file_view(self, fv: FileView) -> None:
        """Install ``fv``, rebuild cached Strips, reset scroll."""
        self._fv = fv
        self._focused_chunk = None
        self._strips = self._render_strips(fv.lines)
        self._base_width = max(fv.widest_line, 1)
        self.virtual_size = Size(self._base_width, fv.line_count)
        self.scroll_to(0, 0, animate=False, immediate=True)
        self.refresh()

    def clear(self) -> None:
        """Empty the buffer."""
        self.set_file_view(FileView())

    def scroll_to_line(self, line_index: int, *, center: bool = False) -> None:
        """Move the viewport so ``line_index`` is visible.

        ``center=False`` (default) puts ``line_index`` at the top of
        the viewport — matches the legacy ``scroll_to_widget(top=True)``
        behaviour. ``center=True`` vertically centres the line so the
        user has context above and below.
        """
        if self._fv is None or self._fv.line_count == 0:
            return
        line_index = max(0, min(line_index, self._fv.line_count - 1))
        target = max(0, line_index - self.size.height // 2) if center else line_index
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

        Updates only the affected Strips in place — no full rebuild.
        """
        if self._focused_chunk == chunk_id or self._fv is None:
            return
        prev = self._focused_chunk
        self._focused_chunk = chunk_id
        if prev is not None:
            self._repaint_chunk_strips(prev)
        if chunk_id is not None:
            self._repaint_chunk_strips(chunk_id)
        self.refresh()

    @property
    def match_lines(self) -> list[int]:
        """Sorted line indices containing matches. Drives the
        scrollbar match markers."""
        if self._fv is None:
            return []
        return sorted(self._fv.match_lines)

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
        return self._strips[line_idx].crop_extend(
            scroll_x, scroll_x + self.size.width, self.rich_style
        )

    # ── Rendering helpers ───────────────────────────────────────

    @staticmethod
    def _render_strips(lines: list[Text]) -> list[Strip]:
        """Convert a list of Rich Text lines to cached Strips.

        We use a single private Console so style resolution is
        consistent across lines. Width is set to a generous ceiling;
        ``render_line`` crops to the current viewport width.
        """
        console = Console(width=10_000, file=None, force_terminal=False)
        strips: list[Strip] = []
        opts = console.options.update(max_width=10_000, overflow="ignore", no_wrap=True)
        for line in lines:
            # ``console.render`` yields ``Segment`` instances; we drop
            # the trailing newline segment and any control segments so
            # the resulting Strip's cell width matches the visible
            # text. Type-narrow to make the dropped items explicit.
            segments: list[Segment] = [
                s for s in console.render(line, opts) if s.text != "\n" and not s.control
            ]
            strips.append(Strip(segments))
        return strips

    def _repaint_chunk_strips(self, chunk_id: int) -> None:
        """Rebuild the strips for every line belonging to ``chunk_id``,
        applying / removing the focused-chunk style as appropriate.

        Touches only the affected slice of ``self._strips``; the
        surrounding lines are untouched.
        """
        if self._fv is None:
            return
        rng = self._fv.chunk_to_range.get(chunk_id)
        if rng is None:
            return
        start, end = rng
        is_focus = chunk_id == self._focused_chunk
        for li in range(start, end):
            base = self._fv.lines[li].copy()
            if is_focus:
                base.stylize(_FOCUSED_CHUNK_STYLE)
            self._strips[li] = self._render_strips([base])[0]

    # ── Selection (multi-line copy) ─────────────────────────────

    def get_selection(self, selection) -> tuple[str, str] | None:  # type: ignore[override]
        """Mirror ``Log.get_selection`` so Textual's clipboard manager
        can extract multi-line selections from the buffer.

        ``selection`` carries ``(start, end)`` as ``(line, col)`` pairs;
        we return ``(text, line_ending)``.
        """
        if self._fv is None or not self._fv.lines:
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
            if not (0 <= y < self._fv.line_count):
                break
            plain = self._fv.lines[y].plain
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
