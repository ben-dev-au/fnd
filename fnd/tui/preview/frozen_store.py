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
from functools import lru_cache
from typing import TYPE_CHECKING

from fnd.tui.preview.frozen import FrozenChunk, FrozenChunkView, FrozenDocument, freeze
from fnd.tui.widgets.markdown import FNDMarkdown

if TYPE_CHECKING:
    from fnd.query import FileChunk
    from fnd.tui.widgets.preview_container import PreviewContainer

__all__ = ["ChunkCaptureStore", "FrozenDocumentStore"]

# How many captured documents to keep. Covers moving between a handful of
# results without the re-decode that PREVIEW_CACHE_MAX_FILES = 1 forces on the
# structural path.
MAX_DOCUMENTS = 4

# Measured on a real corpus: 44.5 KB per captured chunk, 1670 bytes per rendered
# row. Used to price a document in bytes without walking its strips.
BYTES_PER_ROW = 1670

# Share of system memory the preview cache may hold. A document viewer is
# expected to spend memory on the document — PDF readers and editors of this
# kind sit in the hundreds of MB routinely — so this is not trying to be frugal,
# only to leave the machine usable.
CACHE_FRACTION = 0.05

# Floor for machines we cannot measure, or very small ones. Below roughly this
# the cache stops being able to hold a single large file and the feature
# degrades to nothing.
MIN_CACHE_BYTES = 64 * 1024 * 1024

# Ceiling. 5% of a 64 GB workstation would be 3.2 GB, which is past the point of
# being useful and into the point of being rude: a cache holds files already
# read, and holding fifteen large ones warm is already more than a reading
# session touches. Generous, not unbounded.
MAX_CACHE_BYTES = 1024 * 1024 * 1024


@lru_cache(maxsize=1)
def _total_system_bytes() -> int | None:
    """Physical RAM, or ``None`` when it cannot be determined.

    Cached: the answer cannot change while the process runs, and it is read
    once per stored capture — two syscalls per chunk on the sweep's hot loop.
    """
    import os
    import sys

    try:
        if hasattr(os, "sysconf") and "SC_PHYS_PAGES" in os.sysconf_names:
            pages, page_size = os.sysconf("SC_PHYS_PAGES"), os.sysconf("SC_PAGE_SIZE")
            if pages > 0 and page_size > 0:
                return pages * page_size
    except (OSError, ValueError):
        pass
    if sys.platform == "win32":  # pragma: no cover - platform specific
        try:
            import ctypes

            class _MemoryStatusEx(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            status = _MemoryStatusEx()
            status.dwLength = ctypes.sizeof(_MemoryStatusEx)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return int(status.ullTotalPhys)
        except Exception:
            pass
    return None


def budget_rows() -> int:
    """How many captured rows the cache may hold, scaled to this machine.

    A fixed number is wrong in both directions: it starves a user with a
    5,000-page PDF and under-uses a workstation. So it scales with RAM, between
    a floor that keeps the feature working at all and a ceiling that keeps a
    big machine's cache from growing past any real use (1 GB is roughly fifteen
    whole large files).

    Note this bounds the CACHE, not the document on screen: ``put`` never evicts
    the document just stored, so the file being read is served whole however
    large it is. The budget decides how many OTHER files stay warm around it.
    """
    total = _total_system_bytes()
    if total is None:
        budget = MIN_CACHE_BYTES
    else:
        budget = min(MAX_CACHE_BYTES, max(MIN_CACHE_BYTES, int(total * CACHE_FRACTION)))
    return budget // BYTES_PER_ROW


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
        # reading. A file larger than the whole budget is therefore still served
        # whole: the budget bounds how many OTHER files stay warm around it.
        budget = budget_rows()
        while len(self._docs) > 1 and self.total_rows() > budget:
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


class ChunkCaptureStore:
    """Captures held per chunk, so a mount can serve one instead of building it.

    Deliberately NOT a :class:`FrozenDocument`. That type stacks chunks into a
    single row space and so must be contiguous — a gap silently shifts every
    position after it. This holds a sparse SET, because what coverage captures is
    the matches scattered through a file, and nothing here does row arithmetic:
    each capture is looked up by its own chunk_seq and mounted as its own widget.

    Keyed by (file, query, width) for the same reasons as the document store: a
    capture carries the query's highlighting baked in, and its strips are cut at
    one width. A resize does not invalidate anything, it simply misses.

    Bounded in rows against the same machine-scaled budget, evicting whole files
    oldest-first — never the file just written to, which is the one on screen.
    """

    def __init__(self) -> None:
        self._files: OrderedDict[tuple[str, str, int], dict[int, FrozenChunk]] = OrderedDict()
        self._rows = 0

    def get(self, parent_id: str, query_sig: str, width: int, chunk_seq: int) -> FrozenChunk | None:
        key = (parent_id, query_sig, width)
        captures = self._files.get(key)
        if captures is None:
            return None
        capture = captures.get(chunk_seq)
        if capture is not None:
            # Promote on READ, not only on write. Without this the order is
            # purely write order, and coverage writes the current file FIRST and
            # its neighbours after — making the file on screen the OLDEST entry
            # and so the first one evicted. A cache that drops what you are
            # reading in order to hold what you have not opened is worse than no
            # cache; this makes "least recently used" mean what it says.
            self._files.move_to_end(key)
        return capture

    def put(self, parent_id: str, query_sig: str, width: int, capture: FrozenChunk) -> None:
        key = (parent_id, query_sig, width)
        captures = self._files.get(key)
        if captures is None:
            captures = {}
            self._files[key] = captures
        previous = captures.get(capture.chunk_seq)
        if previous is not None:
            self._rows -= previous.height
        captures[capture.chunk_seq] = capture
        self._rows += capture.height
        self._files.move_to_end(key)
        self._evict(budget_rows())

    def _evict(self, budget: int) -> None:
        while len(self._files) > 1 and self._rows > budget:
            _, dropped = self._files.popitem(last=False)
            self._rows -= sum(c.height for c in dropped.values())

    def count(self, parent_id: str, query_sig: str, width: int) -> int:
        return len(self._files.get((parent_id, query_sig, width), {}))

    def total_rows(self) -> int:
        return self._rows

    def clear(self) -> None:
        self._files.clear()
        self._rows = 0

    def drop_other_widths(self, width: int) -> int:
        """Forget captures cut for a different width; return how many files went.

        They can never be served — the key carries the width, so a lookup at the
        current width simply misses them — but they would sit here holding
        strips and evicting captures that ARE usable.
        """
        stale = [k for k in self._files if k[2] != width]
        for key in stale:
            self._rows -= sum(c.height for c in self._files.pop(key).values())
        return len(stale)

    def drop_file(self, parent_id: str) -> None:
        """Forget one file — its content or its highlighting changed."""
        for key in [k for k in self._files if k[0] == parent_id]:
            self._rows -= sum(c.height for c in self._files.pop(key).values())

    def debug_keys(self) -> str:
        """What the store actually holds, for diagnosing a miss."""
        return " ".join(
            f"{pid[:8]}/w{w}/sig{sig[:8]}/n{len(v)}" for (pid, sig, w), v in self._files.items()
        )
