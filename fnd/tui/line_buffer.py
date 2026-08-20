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

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar

from rich.console import Console
from rich.segment import Segment
from rich.text import Text
from textual.strip import Strip

# Dimmed proximity-match swatches (occurrences OUTSIDE a co-occurrence window).
# The auto-scroll target prefers a full, qualifying match so a ``{N}``/``"a b"~N``
# query lands on the actual co-occurrence, not an earlier dimmed lone-term hit.
from fnd.render import DIM_STYLES as _DIM_STYLES
from fnd.tui.strip_document import StripDocumentView

if TYPE_CHECKING:
    pass


# Focused-chunk row band. The legacy line-level match overlay
# (``$accent 8%`` on every row containing a match) used to live here
# too, but is gone by design: matches are highlighted per-word via
# Rich styles baked into ``FileView.lines`` by :func:`build_file_view`,
# which is more readable on documents with dense matches.
_COMPONENT_FOCUSED_CHUNK = "line-buffer--focused-chunk"


# (line_start, line_end_exclusive, kind, payload). Today: kind="chunk", payload=chunk_seq.
StructuralBlock = tuple[int, int, str, object]


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
    # Per-chunk line ranges, ordered by line_start. Populated by the builders.
    structural_map: list[StructuralBlock] = field(default_factory=list)

    @property
    def line_count(self) -> int:
        return len(self.lines)

    @property
    def widest_line(self) -> int:
        return max((line.cell_len for line in self.lines), default=0)


# Match-spans are now styled tuples: ``(byte_start, byte_end, style)``.
# The previous 2-tuple form (start, end) is still accepted — a missing
# style is taken to mean ``"bold"`` so legacy tests continue to pass.
MatchSpan = tuple[int, int] | tuple[int, int, str]

# Default per-word style for 2-tuple match spans (legacy callers that
# don't pass a colour). Vacuum tests use the 2-tuple form, so this is
# ``"bold"`` to match the historical behaviour. Production app.py
# callers pass 3-tuples carrying ``fnd.render.word_highlight_runs``
# styles (yellow ``#ffd866`` for exact, orange for fuzzy).
_DEFAULT_MATCH_STYLE = "bold"


def _span_parts(span: MatchSpan) -> tuple[int, int, str]:
    if len(span) == 3:
        return span[0], span[1], span[2]
    return span[0], span[1], _DEFAULT_MATCH_STYLE


def build_file_view(
    chunks: Sequence[tuple[int, str, Sequence[MatchSpan]]],
    *,
    insert_chunk_gaps: bool = True,
) -> FileView:
    """Construct a FileView from decoded chunk data.

    ``chunks`` is a list of ``(chunk_id, chunk_text, match_spans)``:
      * ``chunk_id``: stable identifier from the search index.
      * ``chunk_text``: the decoded chunk body. Splits on ``\\n``.
      * ``match_spans``: list of ``(byte_start, byte_end)`` ranges OR
        ``(byte_start, byte_end, style)`` triples. Each range is the
        position of a matched word in the chunk text; the optional
        style is the Rich-style string applied to that word
        (e.g. ``"black on #ffd866"``). 2-tuples default to bold so
        existing callers / tests don't need to change.

    Match highlights are **word-level only** — the lines themselves
    are not tinted. The widget's row-level overlays remain available
    via component classes for the focused-chunk band, but a row
    containing a match does NOT get a full-row background by default.

    ``insert_chunk_gaps=True`` adds a blank row between consecutive
    chunks so the user can see where chunks split. The gap row belongs
    to the *following* chunk for offset-mapping purposes.

    Pure function — no widget mounting, no Textual side-effects.
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
        chunk_first_match: int | None = None  # first FULL (qualifying) match line
        chunk_first_dim: int | None = None  # fallback: a dim-only proximity line
        # Per-line list of (local_start, local_end, style) — preserves
        # styles per span so each matched word can render with its own
        # colour (e.g. fuzzy matches differ from exact matches).
        local_styled_offsets: list[list[tuple[int, int, str]]] = [[] for _ in line_offsets]
        for span in match_spans:
            span_start, span_end, span_style = _span_parts(span)
            for li, (lstart, lend, _) in enumerate(line_offsets):
                if span_end <= lstart or span_start >= lend:
                    continue
                local_start = max(0, span_start - lstart)
                local_end = min(lend - lstart, span_end - lstart)
                local_styled_offsets[li].append((local_start, local_end, span_style))
        chunk_start = len(fv.lines)
        # Chunk-boundary gap row (skipped for the very first chunk).
        if insert_chunk_gaps and nth > 0:
            fv.lines.append(Text(""))
            fv.line_to_chunk.append(chunk_id)
        for (_, _, raw_line), styled_spans in zip(line_offsets, local_styled_offsets, strict=False):
            global_line_idx = len(fv.lines)
            t = Text(raw_line)
            if styled_spans:
                for span_start, span_end, span_style in styled_spans:
                    t.stylize(span_style, span_start, span_end)
                fv.match_lines.add(global_line_idx)
                # Prefer a full (qualifying) match for the auto-scroll target; a
                # line whose only matches are dimmed proximity strays is a
                # fallback, so a proximity query lands on the real co-occurrence.
                if any(style not in _DIM_STYLES for _, _, style in styled_spans):
                    if chunk_first_match is None:
                        chunk_first_match = global_line_idx
                elif chunk_first_dim is None:
                    chunk_first_dim = global_line_idx
            fv.lines.append(t)
            fv.line_to_chunk.append(chunk_id)
        chunk_end = len(fv.lines)
        fv.chunk_to_range[chunk_id] = (chunk_start, chunk_end)
        fv.structural_map.append((chunk_start, chunk_end, "chunk", chunk_id))
        target_line = chunk_first_match if chunk_first_match is not None else chunk_first_dim
        if target_line is not None:
            fv.first_hit_line_in_chunk[chunk_id] = target_line
    return fv


@dataclass(slots=True)
class RenderedDocument:
    """FileView + strips at a specific wrap_width. Cacheable, widget-agnostic."""

    fv: FileView
    strips: list[Strip] = field(default_factory=list)
    visual_to_logical: list[int] = field(default_factory=list)
    logical_to_visual_start: list[int] = field(default_factory=list)
    # wrap_width=0 means unwrapped; base_width drives horizontal virtual_size.
    wrap_width: int = 0
    base_width: int = 1

    @property
    def match_lines(self) -> set[int]:
        return self.fv.match_lines

    @property
    def structural_map(self) -> list[StructuralBlock]:
        return self.fv.structural_map


# Report every N lines. Frequent enough that a slow document moves the line
# several times a second, rare enough that the reporting is not itself a cost.
_PROGRESS_EVERY = 64


def build_rendered_document(
    fv: FileView, *, wrap_width: int, on_progress: Callable[[int], None] | None = None
) -> RenderedDocument:
    """Pure: render fv.lines to strips at wrap_width. Safe off-thread."""
    strips, v2l, l2vs = LineBufferPreview._render_lines(
        fv.lines, wrap_width=wrap_width, on_progress=on_progress
    )
    base_width = 1 if wrap_width > 0 else max(fv.widest_line, 1)
    return RenderedDocument(
        fv=fv,
        strips=strips,
        visual_to_logical=v2l,
        logical_to_visual_start=l2vs,
        wrap_width=wrap_width,
        base_width=base_width,
    )


class LineBufferPreview(StripDocumentView):
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

    COMPONENT_CLASSES: ClassVar[set[str]] = {_COMPONENT_FOCUSED_CHUNK}

    DEFAULT_CSS = """
    LineBufferPreview {
        color: $text;
        width: 1fr;
        height: 1fr;
        scrollbar-gutter: stable;
    }
    LineBufferPreview.-hidden { display: none; }
    LineBufferPreview > .line-buffer--focused-chunk {
        background: $accent 15%;
    }
    """

    def __init__(
        self, *, id: str | None = None, wrap: bool = False, show_match_markers: bool = True
    ) -> None:
        super().__init__(id=id, show_match_markers=show_match_markers)
        self._fv: FileView | None = None
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
        # When ``True``, ``set_file_view`` and ``on_resize`` re-wrap the
        # cached Strips to viewport width. The PDF / TXT pipeline turns
        # this on so long tabular lines stay readable without horizontal
        # scrolling. False (default) preserves the existing test-fixture
        # behaviour: lines render verbatim, overflow goes to horizontal
        # scroll.
        self._wrap: bool = wrap
        # Width the cached Strips were wrapped to. ``0`` = unwrapped.
        self._wrap_width: int = 0

    # ── Substrate hooks ─────────────────────────────────────────

    def _visual_row(self, address: int) -> int:
        """Logical line index -> first visual row (the address is wrap-stable;
        the row it lands on is not)."""
        return self._logical_to_visual_y(address)

    def _ready_for_scroll(self) -> bool:
        return bool(self._logical_to_visual_start)

    def _rebuild_for_width(self, width: int) -> bool:
        """Re-wrap to ``width``. Only wrapping mode reflows; an unwrapped buffer
        renders verbatim and overflows to horizontal scroll."""
        if not (self._wrap and self._fv is not None and width > 0 and width != self._wrap_width):
            return False
        # Pass the width through. `_rebuild_strips` otherwise re-reads
        # `self.size`, which on the `on_resize` path is not guaranteed to hold
        # the width the event just reported — so the guard would compare against
        # one value and the wrap be cut at another.
        self._rebuild_strips(width=width)
        return True

    @property
    def match_rows(self) -> list[int]:
        """Sorted visual rows containing matches (one per logical hit)."""
        if self._fv is None:
            return []
        return sorted({self._logical_to_visual_y(li) for li in self._fv.match_lines})

    def address_of_chunk(self, chunk_id: int) -> int | None:
        if self._fv is None:
            return None
        rng = self._fv.chunk_to_range.get(chunk_id)
        return None if rng is None else rng[0]

    def first_match_address_of_chunk(self, chunk_id: int) -> int | None:
        if self._fv is None:
            return None
        return self._fv.first_hit_line_in_chunk.get(chunk_id)

    def top_address(self) -> int | None:
        """The logical (pre-wrap) line at the viewport top — exact across a
        width reflow, which a visual row would not be."""
        if not self._visual_to_logical:
            return None
        y = max(0, min(int(self.scroll_offset.y), len(self._visual_to_logical) - 1))
        return self._visual_to_logical[y]

    # ── Public API ──────────────────────────────────────────────

    def set_file_view(
        self,
        fv: FileView,
        *,
        initial_focus_line: int | None = None,
        center: bool = False,
        context_fraction: float = 0.0,
    ) -> None:
        """Install ``fv`` and scroll to ``initial_focus_line`` synchronously
        (or the top if ``None``) so the first paint is at the right offset."""
        self._fv = fv
        self._focused_chunk = None
        self._pending_scroll_address = None
        self._pending_scroll_center = False
        self._pending_scroll_context_fraction = 0.0
        self._rebuild_strips()
        self._apply_initial_scroll(
            initial_focus_line, center=center, context_fraction=context_fraction
        )
        self._refresh_match_scrollbar()
        self.refresh()
        if self._wrap and self.size.width == 0:
            self.call_after_refresh(self._rebuild_after_layout)

    def set_prebuilt_view(
        self,
        fv: FileView,
        strips: list[Strip],
        visual_to_logical: list[int],
        logical_to_visual_start: list[int],
        *,
        wrap_width: int,
        base_width: int,
        initial_focus_line: int | None = None,
        center: bool = False,
        context_fraction: float = 0.0,
    ) -> None:
        """Install a worker-prerendered view, scrolled to
        ``initial_focus_line`` from the first paint."""
        self._fv = fv
        self._focused_chunk = None
        self._pending_scroll_address = None
        self._pending_scroll_center = False
        self._pending_scroll_context_fraction = 0.0
        self._strips = strips
        self._visual_to_logical = visual_to_logical
        self._logical_to_visual_start = logical_to_visual_start
        self._base_width = base_width
        self._wrap_width = wrap_width
        self._set_extent()
        self._apply_initial_scroll(
            initial_focus_line, center=center, context_fraction=context_fraction
        )
        self._refresh_match_scrollbar()
        self.refresh()
        if self._wrap:
            self.call_after_refresh(self._rebuild_after_layout)

    def _apply_initial_scroll(
        self, line_index: int | None, *, center: bool, context_fraction: float = 0.0
    ) -> None:
        """Set scroll_offset synchronously from logical_to_visual_start so the
        first paint lands at ``line_index``. Queues a pending scroll so a
        post-layout re-wrap can re-apply it if visual_y shifts."""
        if line_index is None or not self._logical_to_visual_start:
            self.scroll_to(0, 0, animate=False, immediate=True)
            return
        clamped = max(0, min(line_index, max(0, self._fv.line_count - 1) if self._fv else 0))
        visual_y = self._logical_to_visual_y(clamped)
        target_y = self._scroll_target_y(visual_y, center=center, context_fraction=context_fraction)
        self.scroll_to(0, target_y, animate=False, immediate=True)
        # Re-apply after layout in case the actual viewport width
        # forces a re-wrap that shifts visual_y.
        self._pending_scroll_address = clamped
        self._pending_scroll_center = center
        self._pending_scroll_context_fraction = context_fraction

    def _rebuild_after_layout(self) -> None:
        if self._fv is None:
            return
        if self.size.width > 0 and self._rebuild_for_width(self.size.width):
            self._refresh_match_scrollbar()
            self.refresh()
        self._apply_pending_scroll()

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

    def scroll_to_line(
        self, line_index: int, *, center: bool = False, context_fraction: float = 0.0
    ) -> None:
        """Scroll the viewport so ``line_index`` is at the top (or centred, or
        ``context_fraction`` down the viewport when matching).

        Safe before layout: the target is remembered and re-applied once
        the buffer has a real viewport.
        """
        if self._fv is None or self._fv.line_count == 0:
            return
        self.scroll_to_address(
            min(line_index, self._fv.line_count - 1),
            center=center,
            context_fraction=context_fraction,
        )

    def top_logical_line(self) -> int | None:
        """The logical (pre-wrap) line index at the current viewport top, or
        ``None`` if nothing is laid out yet. Preserves the reading position
        across a width reflow (e.g. toggling Reading View): capture this, let
        the width change re-wrap, then ``scroll_to_line`` it back exactly."""
        return self.top_address()

    def set_focused_chunk(self, chunk_id: int | None) -> None:
        """Move the focused-chunk accent band; no Strip rebuild."""
        if self._focused_chunk == chunk_id or self._fv is None:
            return
        self._focused_chunk = chunk_id
        self.refresh()

    @property
    def match_lines(self) -> list[int]:
        """Sorted visual row indices containing matches (one per logical hit)."""
        return self.match_rows

    @property
    def file_view(self) -> FileView | None:
        return self._fv

    def _row_overlay_style(self, visual_y: int) -> Any:
        """Return the component-class style overlay for this visual row,
        or ``None`` if the row doesn't need an accent.

        Only the focused-chunk band paints at row level. Match
        highlights are word-level (baked into ``FileView.lines`` at
        build time), so non-focused rows never receive a row overlay.
        """
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
        return None

    # ── Rendering helpers ───────────────────────────────────────

    def _logical_to_visual_y(self, logical_index: int) -> int:
        """Logical line index → first visual row (clamped)."""
        if not self._logical_to_visual_start:
            return 0
        if logical_index <= 0:
            return self._logical_to_visual_start[0]
        if logical_index >= len(self._logical_to_visual_start):
            return self._logical_to_visual_start[-1]
        return self._logical_to_visual_start[logical_index]

    def _rebuild_strips(self, *, width: int | None = None) -> None:
        """Render every logical line to cached Strips for the current wrap."""
        fv = self._fv
        if fv is None:
            self._strips = []
            self._visual_to_logical = []
            self._logical_to_visual_start = []
            self._base_width = 0
            self._set_extent()
            return

        effective = self.size.width if width is None else width
        wrap_width = effective if self._wrap and effective > 0 else 0
        self._wrap_width = wrap_width

        strips, v2l, l2vs = self._render_lines(fv.lines, wrap_width=wrap_width)
        self._strips = strips
        self._visual_to_logical = v2l
        self._logical_to_visual_start = l2vs
        # Report 1-cell virtual width when wrap is on so Textual doesn't
        # paint a phantom horizontal scrollbar.
        self._base_width = 1 if wrap_width > 0 else max(fv.widest_line, 1)
        self._set_extent()

    @staticmethod
    def _render_lines(
        lines: list[Text],
        *,
        wrap_width: int,
        on_progress: Callable[[int], None] | None = None,
    ) -> tuple[list[Strip], list[int], list[int]]:
        """Render lines to Strips. ``wrap_width=0`` disables wrapping.
        Returns (strips, visual_to_logical, logical_to_visual_start).

        ``on_progress`` is called with the number of lines walked so far, every
        :data:`_PROGRESS_EVERY` lines. This is the only real unit of work the
        flat path exposes — everything else about it is one opaque call — so
        the progress line reads it rather than estimating a duration it cannot
        predict. Off by default and free when unset.
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
            if on_progress is not None and li % _PROGRESS_EVERY == 0:
                on_progress(li)
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
