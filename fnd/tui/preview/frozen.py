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

from textual.events import Resize
from textual.geometry import Size
from textual.message import Message
from textual.strip import Strip
from textual.widget import Widget

from fnd.tui.preview.match_row import chunk_stop_rows, row_within, rows_to_first_match


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
    cell_rows: dict[tuple[int, int, int], int] = field(default_factory=dict)

    @property
    def outer_height(self) -> int:
        """Rows the chunk occupied, padding included: the strips are its whole
        box, so the stand-in adds nothing to this and the row fields index the
        strips directly."""
        return len(self.strips)


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
        # A threshold, not equality: Textual ANIMATES opacity, so an ancestor
        # part-way through a fade reports a small non-zero value and captures
        # just as blank as one at exactly zero.
        if (
            getattr(node, "styles", None) is not None
            and getattr(node.styles, "opacity", 1.0) <= 0.01
        ):
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
    cell_rows: dict[tuple[int, int, int], int] = {}
    if isinstance(chunk, FNDMarkdown):
        inner = chunk.first_match_block
        # The chunk's own spec, so this asks exactly what enumerate_stop_regions
        # asks of the live blocks — a block whose spans were cleared after it
        # registered would otherwise get a scanned row live and row 0 frozen.
        spec = chunk.match_spec
        if inner is not None and (block_row := row_within(inner, chunk)) is not None:
            # Down to the row the match PAINTS on, not the block's top: 32 rows
            # apart on a wrapped contents page, and the live scroll counts the
            # same way, so the substrates cannot disagree.
            first_match_row = block_row + rows_to_first_match(inner, spec)
        # The same pass the live scroll resolves a last-match entry with, so the
        # two substrates cannot disagree about which stop is last.
        stop_rows, cell_rows = chunk_stop_rows(chunk, spec)
        if first_match_row is None and stop_rows:
            first_match_row = stop_rows[0]

    # Capture the VIRTUAL height, not the allocated one: they differ (48 vs 56
    # rows on one measured chunk) and using ``size`` silently truncates the tail.
    #
    # Plus the chunk's own gutter, because ``Compositor.reflow`` lays a root's
    # children out inside the size it is given MINUS that root's padding. The
    # strips are therefore the chunk's whole box, padding rows included.
    full = Size(
        size.width, max(size.height, chunk.virtual_size.height) + chunk.styles.gutter.height
    )
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

    class WidthStale(Message):
        """This view is painting strips cut for a width it is no longer laid
        out at, so its rows are being cropped (or padded) rather than re-wrapped.

        Posted by the view because the view is the only thing that knows. The
        pane-level resize hook runs from ``call_after_refresh``, which fires
        BEFORE the re-layout — measured, the pane still reports its old width
        and every chunk its old size at that point, so nothing looks stale from
        there and no repair ever ran. A widget's own Resize arrives after it has
        been laid out, with the new size in hand.
        """

        def __init__(self, view: FrozenChunkView) -> None:
            super().__init__()
            self.view = view

    def on_resize(self, event: Resize) -> None:
        # Strips cannot be re-wrapped, so this cannot repair itself — it can
        # only say so, and let the presenter decide.
        #
        # Once per width, not once per event: a window drag emits a resize per
        # column to every mounted chunk, measured at 522 reports for a single
        # twelve-column gesture.
        width = event.size.width
        if width <= 0 or width == self._reported_width:
            return
        self._reported_width = width
        if width != self.frozen.width:
            self.post_message(self.WidthStale(self))

    def __init__(self, frozen: FrozenChunk, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self.frozen = frozen
        # A fixed height, equal to what the widget tree occupied — so swapping
        # the tree for this moves nothing on screen and needs no scroll
        # compensation, unlike removing the chunk outright. The strips are the
        # chunk's whole box, padding rows included, so this widget carries no
        # padding of its own: adding any would paint the content that much low
        # and clip the same number of rows off the bottom.
        self.styles.height = frozen.outer_height
        # Read by the scroll strategy in place of descending into a widget tree.
        self.fnd_first_match_row = frozen.first_match_row
        # Last width this view reported as stale, so a drag reports once per
        # column rather than once per resize event.
        self._reported_width: int = 0

    def adopt(self, frozen: FrozenChunk) -> None:
        """Swap in strips cut at a different width, in place.

        A resize changes presentation, not content — so the fix is to replace
        the strips rather than the widget. Nothing is unmounted, so the pane
        cannot blank and the mounted run cannot develop a hole.
        """
        self.frozen = frozen
        self.styles.height = frozen.outer_height
        self.fnd_first_match_row = frozen.first_match_row
        self._reported_width = frozen.width
        self.refresh(layout=True)

    def render_line(self, y: int) -> Strip:
        strips = self.frozen.strips
        if 0 <= y < len(strips):
            return strips[y].crop(0, self.size.width)
        return Strip.blank(self.size.width)
