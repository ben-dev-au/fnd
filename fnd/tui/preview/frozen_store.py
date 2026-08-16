"""Per-file cache of captured documents, harvested from the mount pipeline.

Freezing already happens on every file the user reads — the backfill sweep
captures each chunk outside the visible window and drops its widget tree. Those
captures were being thrown away with the container. Keeping them turns a second
visit to the same file into a scroll rather than a rebuild, and gives warming
something to prepend into.

A document holds a CONTIGUOUS run of chunks, never a set with holes. Every row
position downstream (a jump, a marker, ``n``/``b``) is computed by accumulating
chunk heights from the top, so a gap silently shifts every position after it —
nothing raises, the matches simply land in the wrong place.

Contiguous is the right rule rather than complete because complete is rarely
available: the background fill stops the moment the user takes scroll control,
so a 41-chunk file typically has 40 mounted. A run is self-consistent — its rows
describe the slice it holds — so it can be served for any chunk inside it and
grown at either end later. The window chunks the sweep deliberately leaves live
are captured without being removed: the document is a shadow copy, not the thing
on screen.

Captures are width-locked, so the key carries the width the strips were rendered
at. A resize does not invalidate the cache; it simply misses.
"""

from __future__ import annotations

from collections import OrderedDict
from typing import TYPE_CHECKING

from fnd.tui.preview.frozen import FrozenChunk, FrozenChunkView, FrozenDocument, freeze
from fnd.tui.widgets.markdown import FNDMarkdown

if TYPE_CHECKING:
    from fnd.query import FileChunk
    from fnd.tui.widgets.preview_container import PreviewContainer

__all__ = ["FrozenDocumentStore"]

# How many captured documents to keep. Covers moving between a handful of
# results without the re-decode that PREVIEW_CACHE_MAX_FILES = 1 forces on the
# structural path.
MAX_DOCUMENTS = 4

# The real bound. A count of documents was the only cap while a document held a
# handful of chunks; warming grows one to the whole file, and measured on the
# real corpus a captured chunk costs 44.5 KB (1670 bytes per row), so a
# 1463-chunk file is 63.5 MB and four of them 254 MB. Rows are what actually
# grow, so rows are what is budgeted: 20,000 is roughly 33 MB, enough for one
# large file plus neighbours.
MAX_TOTAL_ROWS = 20_000


class FrozenDocumentStore:
    """Captured whole-file documents, keyed by (file, query, width)."""

    def __init__(self) -> None:
        self._docs: OrderedDict[tuple[str, str, int], FrozenDocument] = OrderedDict()

    def get(self, parent_id: str, query_sig: str, width: int) -> FrozenDocument | None:
        key = (parent_id, query_sig, width)
        doc = self._docs.get(key)
        if doc is not None:
            self._docs.move_to_end(key)
        return doc

    def put(self, parent_id: str, query_sig: str, width: int, doc: FrozenDocument) -> None:
        # A put always carries a settled width, which makes it the reliable
        # moment to evict captures cut for a different one. Doing it on the
        # resize event instead is not reliable: that fires before layout, so the
        # width read there is still the old one.
        self.drop_other_widths(width)
        key = (parent_id, query_sig, width)
        self._docs[key] = doc
        self._docs.move_to_end(key)
        while len(self._docs) > MAX_DOCUMENTS:
            self._docs.popitem(last=False)
        # Evict by ROWS, oldest first, never the document just stored — it is
        # the one on screen, and dropping it would rebuild what the user is
        # reading. A single file over budget is therefore kept: the cap bounds
        # the cache, not the current document.
        while len(self._docs) > 1 and self.total_rows() > MAX_TOTAL_ROWS:
            self._docs.popitem(last=False)

    def total_rows(self) -> int:
        return sum(d.total_rows for d in self._docs.values())

    def clear(self) -> None:
        self._docs.clear()

    def drop_other_widths(self, width: int) -> int:
        """Forget captures cut for a different width; return how many went.

        They can never be served — the key carries the width, so a lookup at the
        current width simply misses them — but they would sit in the cache
        holding a whole file's strips and evicting captures that ARE usable.
        """
        stale = [k for k in self._docs if k[2] != width]
        for key in stale:
            del self._docs[key]
        return len(stale)

    def drop_file(self, parent_id: str) -> None:
        """Forget one file — its content or its highlighting changed."""
        for key in [k for k in self._docs if k[0] == parent_id]:
            del self._docs[key]

    def capture(
        self,
        container: PreviewContainer,
        chunks: list[FileChunk],
    ) -> FrozenDocument | None:
        """Assemble the longest contiguous run of capturable chunks.

        Already-frozen chunks contribute their capture directly; live ones are
        captured without being disturbed. A chunk breaks the run when it is not
        mounted, or holds a nested scroll region that a flat run of strips
        cannot express. Returns the longest run found, or ``None`` if nothing is
        capturable.
        """
        best: FrozenDocument | None = None
        current = FrozenDocument()
        for chunk in chunks:
            widget = container.chunk_widgets.get(chunk.chunk_seq)
            captured: FrozenChunk | None = None
            if isinstance(widget, FrozenChunkView):
                captured = widget.frozen
            elif isinstance(widget, FNDMarkdown):
                captured = freeze(widget, chunk.chunk_seq)
            if captured is None:
                if best is None or len(current.chunks) > len(best.chunks):
                    best = current if current.chunks else best
                current = FrozenDocument()
                continue
            current.append(captured)
        if current.chunks and (best is None or len(current.chunks) > len(best.chunks)):
            best = current
        return best
