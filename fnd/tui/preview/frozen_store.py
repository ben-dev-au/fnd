"""Per-chunk cache of captured previews, harvested from the mount pipeline.

Freezing already happens on every file the user reads — the backfill sweep
captures each chunk outside the visible window and drops its widget tree. Those
captures used to be thrown away with the container, so a jump back to a match
cost exactly as much as reaching it the first time. Keeping them turns a revisit
into a mount rather than a rebuild.

Captures are held per CHUNK, sparsely: what navigation reaches is the matches
scattered through a file, and each is looked up by its own ``chunk_seq`` and
mounted as its own widget, so nothing here does row arithmetic and a gap in the
set costs nothing.

Captures are width-locked, so the key carries the width the strips were rendered
at — the width a chunk actually lays out at, which is the pane's scrollable
content width and therefore moves when a scrollbar appears. A capture cut for one
width can never be served at another, so a width change is a miss by design; the
resize sweep drops what it can, and see `invalidate_captures_on_resize` for the
case it cannot see.
"""

from __future__ import annotations

from collections import OrderedDict
from functools import lru_cache

from fnd.tui.preview.frozen import FrozenChunk

__all__ = ["ChunkCaptureStore"]

# Measured on a real corpus: 44.5 KB per captured chunk, 1670 bytes per rendered
# row. Used to price a capture in bytes without walking its strips.
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


class ChunkCaptureStore:
    """Captures held per chunk, so a mount can serve one instead of building it.

    Deliberately sparse rather than a contiguous run. A run stacks chunks into a
    single row space and so must be contiguous — a gap silently shifts every
    position after it. This holds a sparse SET, because what coverage captures is
    the matches scattered through a file, and nothing here does row arithmetic:
    each capture is looked up by its own chunk_seq and mounted as its own widget.

    Keyed by (file, query, width): a
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

    def has(self, parent_id: str, query_sig: str, width: int, chunk_seq: int) -> bool:
        """Is this chunk held? Deliberately does NOT promote.

        Probing readiness is not using a capture. The results list asks this
        for every file twice a second to paint the warmth arrows, and answering
        through :meth:`get` re-ordered the whole store on results-list order —
        neutralising the read-promotion above and leaving the file on screen,
        usually the top result, as the first eviction victim.
        """
        return chunk_seq in self._files.get((parent_id, query_sig, width), {})

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
