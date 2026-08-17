"""Freeze a built chunk: keep what it painted, drop the widgets that painted it.

The preview's cost is not rendering markdown, it is holding widgets. Textual's
arrange is linear in widget count, and a chunk is tens of them — measured 42 per
chunk on a real PDF. Freezing keeps the rendered result and discards the tree:

    arrange, 400 chunks   widget trees 41.7ms   frozen 2.1ms

Fidelity is guaranteed by construction rather than by re-implementing anything:
the chunk is still built by the real :class:`FNDMarkdown`, and the strips are
that tree's own output. Tables, fenced code, list bullets, inline formatting and
the match highlighting all survive because none of them are re-derived.

Capture drives a ``Compositor`` directly. ``Widget.render_lines`` renders only a
widget's OWN content — children are composed over it by the Screen — so
capturing a container that way yields strips that are styled but EMPTY, which
looks exactly like the technique not working. ``Compositor.render_strips`` takes
an explicit size, so a chunk taller than the terminal captures in full.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass, field
from typing import Any

from textual.geometry import Size
from textual.strip import Strip
from textual.widget import Widget

from fnd.tui.strip_document import StripDocumentView


@dataclass(slots=True)
class FrozenChunk:
    """What a built chunk painted, plus the positions the preview navigates by.

    The row fields are captured while the widgets still exist, which is the point:
    resolving a match — or a table cell — against a live tree is what races today
    (a DataTable's cell region is unresolvable until its rows lay out). A row
    number recorded at capture time cannot race.
    """

    chunk_seq: int
    width: int
    strips: list[Strip]
    first_match_row: int | None = None
    stop_rows: list[int] = field(default_factory=list)
    cell_rows: dict[tuple[int, int], int] = field(default_factory=dict)
    # The chunk's own padding, in Textual's (top, right, bottom, left) order.
    # Captured because the strips are the CONTENT region only: a chunk carries
    # `.chunk-section` / `.chunk-first` padding, so a stand-in without it is a
    # row shorter than the widget it replaces.
    padding: tuple[int, int, int, int] = (0, 0, 0, 0)

    @property
    def height(self) -> int:
        """Rows of content. The stand-in adds ``padding`` on top of this."""
        return len(self.strips)

    @property
    def outer_height(self) -> int:
        """Rows the chunk occupied in its container, padding included."""
        return len(self.strips) + self.padding[0] + self.padding[2]

    def is_valid_for(self, width: int) -> bool:
        """Strips are width-locked, so a reflow invalidates the capture."""
        return width == self.width


def _row_within(widget: Widget, chunk: Widget) -> int | None:
    try:
        r, c = widget.region, chunk.region
    except Exception:
        return None
    if r.height == 0 or c.height == 0:
        return None
    return r.y - c.y


def freeze(chunk: Widget, chunk_seq: int) -> FrozenChunk | None:
    """Capture ``chunk`` — built and laid out — as strips plus positions.

    ``None`` means "not capturable, leave it live": either it is not laid out
    yet, or it contains a widget that scrolls INSIDE the chunk. A nested
    viewport cannot be flattened onto a run of strips — the capture would hold
    only the rows currently on screen — so such chunks stay as widgets. In
    practice that is now nothing: Textual's DataTable used to cap itself at one
    viewport height, and lifting that (see FNDMarkdownTableDT) took the share of
    unfreezable chunks on a real corpus from 3.4% to zero. The guard stays
    because the invariant, not the current rate, is what makes this safe.
    """
    from textual._compositor import Compositor
    from textual.widgets import DataTable

    from fnd.tui.widgets.markdown import FNDMarkdown

    size = chunk.size
    if size.height == 0 or size.width == 0:
        return None
    # Refuse anything not actually being PAINTED. A hidden widget keeps its
    # geometry, so the size check above passes, but the compositor renders it
    # blank — the capture then holds a correctly-sized run of empty strips and
    # nothing downstream can tell. The background fill hides widgets while it
    # works and restores them in a `finally` that runs AFTER the freeze sweep,
    # so this is reachable on every file whose tail is filled that way, and it
    # shows up as matches near the end of a file rendering blank.
    node: Widget | None = chunk
    while node is not None:
        if not getattr(node, "display", True):
            return None
        if getattr(node, "styles", None) is not None and getattr(node.styles, "opacity", 1.0) == 0:
            return None
        node = node.parent if isinstance(node.parent, Widget) else None
    for dt in chunk.query(DataTable):
        # A table that holds rows but has NO geometry has not been laid out, and
        # capturing it yields the border with none of the contents — an empty
        # box, indistinguishable downstream from a table that is genuinely
        # empty. The nested-scroll check below cannot see this: an unlaid table
        # measures 0 both ways, so `virtual > size` is `0 > 0` and passes.
        if dt.row_count > 0 and (dt.size.height <= 0 or dt.virtual_size.height <= 0):
            return None
        if dt.virtual_size.height > dt.size.height or dt.virtual_size.width > dt.size.width:
            return None

    # Positions first: they need the widgets, which the caller is about to drop.
    first_match_row: int | None = None
    stop_rows: list[int] = []
    cell_rows: dict[tuple[int, int], int] = {}
    if isinstance(chunk, FNDMarkdown):
        inner = chunk.first_match_block
        if inner is not None:
            first_match_row = _row_within(inner, chunk)
        from fnd.tui.widgets.markdown import FNDMarkdownTableDT, FNDMarkdownTD, FNDMarkdownTH

        for block in chunk.match_blocks:
            # Skip a table's own cell blocks, exactly as enumerate_stop_regions
            # does: the table owns those cells and they are collected below from
            # ``_fnd_match_coords``. Counting both double-counts every table
            # match, which showed up as a frozen chunk reporting 6 stops where
            # the live one reported 5.
            #
            # The ``sorted(set(...))`` below masks this whenever a cell block's
            # row equals the row derived from the DataTable — measured, that is
            # the common case, so removing this skip does NOT fail the parity
            # test. It stays because the two disagree once a cell spans rows,
            # and a silent extra stop is the failure mode this whole path exists
            # to prevent.
            if isinstance(block, FNDMarkdownTableDT | FNDMarkdownTD | FNDMarkdownTH):
                continue
            row = _row_within(block, chunk)
            if row is not None:
                stop_rows.append(row)
        for dt in chunk.query(DataTable):
            base = _row_within(dt, chunk)
            if base is None:
                continue
            for coord in getattr(dt, "_fnd_match_coords", []) or []:
                try:
                    cell = dt._get_cell_region(coord)  # pyright: ignore[reportAttributeAccessIssue]
                except Exception:
                    continue
                if cell.height == 0:
                    continue
                cell_rows[(coord.row, coord.column)] = base + cell.y - dt.scroll_offset.y
        stop_rows.extend(cell_rows.values())
        stop_rows = sorted(set(stop_rows))
        if first_match_row is None and stop_rows:
            first_match_row = stop_rows[0]

    # Capture the VIRTUAL height, not the allocated one: they differ (48 vs 56
    # rows on one measured chunk) and using ``size`` silently truncates the tail.
    full = Size(size.width, max(size.height, chunk.virtual_size.height))
    comp = Compositor()
    comp.reflow(chunk, full)
    pad = chunk.styles.padding
    return FrozenChunk(
        chunk_seq=chunk_seq,
        width=full.width,
        strips=comp.render_strips(full),
        first_match_row=first_match_row,
        stop_rows=stop_rows,
        cell_rows=cell_rows,
        padding=(pad.top, pad.right, pad.bottom, pad.left),
    )


class FrozenChunkView(Widget):
    """One widget standing in for a frozen chunk's whole widget tree.

    Deliberately one widget PER CHUNK rather than one per document. Everything
    the preview addresses is per chunk — ``chunk_widgets``, ``match_targets``,
    the scroll strategy, lazy mount, prune — and all of it keeps working if a
    chunk stays a widget and merely stops being a *tree* of them. Measured at 400
    chunks: 41.7ms arrange today, 2.1ms this way, 0.04ms as a single
    document-wide widget. The last is another 50x, but it needs every one of
    those mechanisms rewritten to buy a difference well under one frame.
    """

    DEFAULT_CSS = """
    FrozenChunkView {
        width: 1fr;
    }
    """

    def __init__(self, frozen: FrozenChunk, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self.frozen = frozen
        # A fixed height, equal to what the widget tree occupied — so swapping
        # the tree for this moves nothing on screen and needs no scroll
        # compensation, unlike removing the chunk outright.
        #
        # "What it occupied" includes the chunk's padding. The strips are the
        # CONTENT region, and a chunk carries `.chunk-section` (one row below)
        # or `.chunk-first` (one row above), so a stand-in sized to the strips
        # alone is a row short. That row is invisible on its own and lethal in
        # aggregate: the sweep freezes chunks ABOVE the viewport too, and
        # shrinking the content above without touching scroll_y slides what the
        # user is reading upward — measured at exactly -6 rows for 6 chunks
        # frozen above, a second or two after the navigation landed.
        top, right, bottom, left = frozen.padding
        self.styles.padding = (top, right, bottom, left)
        self.styles.height = frozen.outer_height
        # Read by the scroll strategy in place of descending into a widget tree.
        self.fnd_first_match_row = frozen.first_match_row

    def render_line(self, y: int) -> Strip:
        strips = self.frozen.strips
        if 0 <= y < len(strips):
            return strips[y].crop(0, self.size.width)
        return Strip.blank(self.size.width)


@dataclass(slots=True)
class FrozenDocument:
    """Captured chunks in document order, plus the row each one starts at.

    ``starts[i]`` is the first row of ``chunks[i]`` in document space, kept as a
    parallel sorted list so row -> chunk is a bisect rather than a scan. The
    preview asks that question on every scroll.
    """

    chunks: list[FrozenChunk] = field(default_factory=list)
    starts: list[int] = field(default_factory=list)
    total_rows: int = 0
    width: int = 0

    def append(self, chunk: FrozenChunk) -> None:
        self.width = chunk.width
        self.starts.append(self.total_rows)
        self.chunks.append(chunk)
        self.total_rows += chunk.height

    def prepend(self, chunk: FrozenChunk) -> None:
        self.width = chunk.width
        self.starts = [0, *(s + chunk.height for s in self.starts)]
        self.chunks.insert(0, chunk)
        self.total_rows += chunk.height

    def _start_of(self, chunk_seq: int) -> tuple[int, FrozenChunk] | None:
        for start, c in zip(self.starts, self.chunks, strict=True):
            if c.chunk_seq == chunk_seq:
                return start, c
        return None

    def row_of_chunk(self, chunk_seq: int) -> int | None:
        found = self._start_of(chunk_seq)
        return None if found is None else found[0]

    def match_row(self, chunk_seq: int) -> int | None:
        """Document row of a chunk's first match — where a navigation lands."""
        found = self._start_of(chunk_seq)
        if found is None:
            return None
        start, chunk = found
        return start + (chunk.first_match_row or 0)

    def cell_row(self, chunk_seq: int, coord: tuple[int, int]) -> int | None:
        """Document row of a matched table cell.

        Recorded at capture time, so it cannot be unresolvable the way a live
        ``DataTable`` cell region is until its rows lay out — the race behind the
        deep-table scroll history.
        """
        found = self._start_of(chunk_seq)
        if found is None:
            return None
        start, chunk = found
        off = chunk.cell_rows.get(coord)
        return None if off is None else start + off

    def stop_rows(self) -> list[int]:
        """Every match stop in document order — what n/b and the markers walk."""
        out: list[int] = []
        for start, c in zip(self.starts, self.chunks, strict=True):
            out.extend(start + r for r in c.stop_rows)
        return sorted(out)

    def chunk_at_row(self, row: int) -> int | None:
        if not self.chunks:
            return None
        i = bisect_right(self.starts, row) - 1
        return None if i < 0 else self.chunks[i].chunk_seq

    def line(self, row: int) -> Strip | None:
        if not (0 <= row < self.total_rows):
            return None
        i = bisect_right(self.starts, row) - 1
        return self.chunks[i].strips[row - self.starts[i]]


class FrozenDocumentView(StripDocumentView):
    """A whole file as one widget: captured strips, served by row.

    Exists for a reason beyond widget count. Adding content ABOVE the viewport is
    what makes warming a file visible, and only a widget that OWNS its
    ``virtual_size`` can grow and scroll atomically — content, size and offset in
    one synchronous block, with no layout pass between them and nothing to clamp
    against. A container's virtual size is assigned BY the layout pass
    (``Widget._size_updated``), so its compensating scroll is always validated
    against a stale extent: measured, a 7-row error, or three frames of drift if
    corrected afterwards.

    ``ScrollView`` is built for exactly this — it discards the compositor's
    virtual size in ``_size_updated`` and overrides ``scroll_to`` to skip the
    ``call_after_refresh`` deferral — and every stock line-API widget (RichLog,
    DataTable, Tree, TextArea) is the same shape.

    A document row IS the address here: unlike the flat buffer there is no wrap
    step between the two. What the base gives us on top of that is the viewport
    paint, the scroll that survives being called before layout, the match markers
    and multi-line selection — none of which is substrate-specific.
    """

    DEFAULT_CSS = """
    FrozenDocumentView {
        width: 1fr;
        height: 1fr;
        scrollbar-gutter: stable;
    }
    FrozenDocumentView.-hidden { display: none; }
    """

    def __init__(self, document: FrozenDocument, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.document = document
        self._sync()

    def _sync(self) -> None:
        self._strips = [s for chunk in self.document.chunks for s in chunk.strips]
        self._base_width = max(self.document.width, 1)
        self._set_extent()

    def set_document(self, document: FrozenDocument) -> None:
        """Show a different file. One widget is reused across files — mounting a
        fresh one per navigation is the DOM churn this substrate exists to
        avoid.

        Resets the scroll. Reusing the widget means it carries the PREVIOUS
        file's offset otherwise, so the new file paints at a position that means
        nothing in it — a settled frame at the wrong place, then the jump to the
        match; the caller scrolls to the match immediately afterwards.
        """
        self.document = document
        self._sync()
        self.set_reactive(FrozenDocumentView.scroll_x, 0.0)
        self.set_reactive(FrozenDocumentView.scroll_y, 0.0)
        self.scroll_target_x = 0.0
        self.scroll_target_y = 0.0
        # set_reactive: assign without running validators or watchers, which at
        # this moment would clamp against the OUTGOING document's extent.
        self._refresh_match_scrollbar()
        self.refresh()

    @property
    def match_rows(self) -> list[int]:
        return self.document.stop_rows()

    def row_of_chunk(self, chunk_id: int) -> int | None:
        return self.document.row_of_chunk(chunk_id)

    def first_match_row_of_chunk(self, chunk_id: int) -> int | None:
        return self.document.match_row(chunk_id)

    def _rebuild_for_width(self, width: int) -> bool:
        """Strips are width-locked and cannot be re-wrapped.

        The flat buffer re-renders its text lines at the new width; a capture
        has no text to re-render — re-deriving it would mean rebuilding the
        markdown widget tree, which is the cost the substrate exists to avoid.
        So a resize invalidates rather than reflows: the view reports itself
        stale and the presenter rebuilds through the widget path, which
        re-captures at the new width on its way.

        Returns False because nothing was re-rendered; the caller must not
        assume the strips now match ``width``.
        """
        return False

    def is_stale_for(self, width: int) -> bool:
        """Whether the captured strips still match the width they must paint at."""
        return bool(self.document.chunks) and self.document.width != width

    # ── Growing in either direction ─────────────────────────────

    def append(self, chunk: FrozenChunk) -> None:
        """Add below. Never shifts the view, so there is nothing to compensate."""
        self.document.append(chunk)
        self._strips.extend(chunk.strips)
        self._base_width = max(self.document.width, 1)
        self._set_extent()

    def prepend(self, chunk: FrozenChunk) -> None:
        """Add above WITHOUT the view moving.

        Order is load-bearing: ``virtual_size`` first, then the scroll. Reversed,
        ``validate_scroll_y`` clamps against the old extent and the view drifts by
        exactly the clamp. ``scroll_to`` keeps ``scroll_y`` and ``scroll_target_y``
        in step, so a later relative scroll starts from the right place.

        The compensating scroll is issued directly rather than through
        ``scroll_to_address``: that path can defer itself to a later refresh when
        the widget is not laid out yet, and a deferred compensation is exactly
        the drift this method exists to avoid.
        """
        self.document.prepend(chunk)
        self._strips[:0] = chunk.strips
        self._base_width = max(self.document.width, 1)
        self._set_extent()
        self.scroll_to(y=int(self.scroll_offset.y) + chunk.height, animate=False, immediate=True)

    def scroll_to_row(self, row: int, *, context_fraction: float = 0.25) -> None:
        """Drop ``row`` a fraction down the viewport, matching where the widget
        path lands a match, so the two substrates look the same."""
        self.scroll_to_address(row, context_fraction=context_fraction)

    def scroll_to_chunk_seq(self, chunk_seq: int, *, prefer_match: bool = True) -> bool:
        """``scroll_to_chunk`` with a found/not-found answer, which the preview
        needs in order to fall back when a chunk is not warmed yet."""
        row = self.document.match_row(chunk_seq) if prefer_match else None
        if row is None:
            row = self.document.row_of_chunk(chunk_seq)
        if row is None:
            return False
        self.scroll_to_row(row)
        return True
