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

from dataclasses import dataclass, field

from textual.geometry import Size
from textual.strip import Strip
from textual.widget import Widget


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

    @property
    def height(self) -> int:
        return len(self.strips)

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
    for dt in chunk.query(DataTable):
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
        for block in chunk.match_blocks:
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
    return FrozenChunk(
        chunk_seq=chunk_seq,
        width=full.width,
        strips=comp.render_strips(full),
        first_match_row=first_match_row,
        stop_rows=stop_rows,
        cell_rows=cell_rows,
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
        self.styles.height = frozen.height
        # Read by the scroll strategy in place of descending into a widget tree.
        self.fnd_first_match_row = frozen.first_match_row

    def render_line(self, y: int) -> Strip:
        strips = self.frozen.strips
        if 0 <= y < len(strips):
            return strips[y].crop(0, self.size.width)
        return Strip.blank(self.size.width)
