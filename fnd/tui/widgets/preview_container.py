"""Per-file preview containers and their LRU cache."""

from __future__ import annotations

from collections import OrderedDict
from typing import Any

from textual.containers import Container
from textual.widget import Widget

from fnd.tui.preview import tuning

__all__ = [
    "PreviewCache",
    "PreviewContainer",
    "_HitWithQuery",
]


class PreviewContainer(Container):
    """Per-file preview container holding the mounted chunk widgets.

    One container per cached file lives inside ``#preview_pane``; only
    one is ``-active`` (visible) at a time. Switching files toggles
    classes — no remount cost. Each container tracks which chunk
    indices have been mounted so a partial-then-cancelled mount can be
    resumed on revisit.
    """

    # display:none required; visibility:hidden leaves containers in
    # vertical flow and collapses the active LineBufferPreview height.
    DEFAULT_CSS = """
    PreviewContainer { width: 100%; height: auto; }
    PreviewContainer.-hidden { display: none; }
    /* opacity:0 (not visibility:hidden) so the pane can be scrolled to the
       match WHILE the container is invisible — scroll_to_region is a no-op
       under visibility:hidden, which forced a reveal-at-top-then-scroll jump.
       The reveal lands only after the scroll commits (see _finalize_via_lock). */
    PreviewContainer.-pre-reveal { opacity: 0%; }
    """

    def __init__(
        self,
        *,
        parent_doc_id: str,
        query_signature: str,
        total_chunks: int,
    ) -> None:
        super().__init__()
        self.parent_doc_id = parent_doc_id
        self.query_signature = query_signature
        self.total_chunks = total_chunks
        self.mounted_indices: set[int] = set()
        # chunk_seq → first widget for that chunk (the header / title row).
        self.chunk_widgets: dict[int, Widget] = {}
        # chunk_seq → first match-bearing widget (or header when no match).
        self.match_targets: dict[int, Widget] = {}

    @property
    def is_complete(self) -> bool:
        return len(self.mounted_indices) >= self.total_chunks

    def get_content_height(self, container, viewport, width):  # type: ignore[no-untyped-def]
        # Textual's VerticalLayout has an "all children are dynamic
        # height" shortcut that arranges them inside ``container.height``
        # — fine for flex-style nested containers, wrong here because
        # this widget is the scrollable canvas of #preview_pane. The
        # shortcut caps total height at the pane's viewport height
        # (~19 cells), so chunks past that y are positioned in widget
        # coords but unreachable via scroll. Force the non-shortcut
        # branch by arranging with height=0, which lets each FNDMarkdown
        # child report its full intrinsic height.
        if not self.children:
            return 0
        from textual.geometry import Size as _Size

        arrangement = self.arrange(_Size(width, 0))
        return arrangement.total_region.height


class _HitWithQuery:
    """Adapter exposing a Hit plus the current query string as one object
    for ``OpenWithScreen``. The modal's :func:`build_request` reads
    ``hit.query`` to populate the Skim ``&search=`` highlight; Hit
    itself doesn't carry the query."""

    __slots__ = ("_hit", "query")

    def __init__(self, hit: Any, query: str) -> None:
        self._hit = hit
        self.query = query

    def __getattr__(self, name: str) -> Any:
        # Forward everything not explicitly overridden to the wrapped Hit.
        return getattr(self._hit, name)


class PreviewCache:
    """LRU cache of :class:`PreviewContainer`, keyed by
    ``(parent_doc_id, query_signature)``. Files with fewer than
    :data:`tuning.PREVIEW_CACHE_MIN_CHUNKS` chunks are NOT cached — they
    mount fast enough that keeping the widget tree alive isn't worth
    the memory.
    """

    def __init__(
        self,
        *,
        max_files: int = tuning.PREVIEW_CACHE_MAX_FILES,
        min_chunks: int = tuning.PREVIEW_CACHE_MIN_CHUNKS,
    ) -> None:
        self._cache: OrderedDict[tuple[str, str], PreviewContainer] = OrderedDict()
        self.max_files = max_files
        self.min_chunks = min_chunks

    def get(self, parent_doc_id: str, query_signature: str) -> PreviewContainer | None:
        key = (parent_doc_id, query_signature)
        container = self._cache.get(key)
        if container is not None:
            self._cache.move_to_end(key)
        return container

    def put(
        self,
        container: PreviewContainer,
        *,
        protect: PreviewContainer | None = None,
    ) -> list[PreviewContainer]:
        """Cache ``container`` and return any LRU-evicted containers for the
        caller to remove. ``protect`` is skipped during eviction.

        ``protect`` is load-bearing, not just tidy: a STALE mount — one an
        overshoot-and-return navigation cancelled — runs its ``finally`` LATE,
        after a newer navigation has already re-activated a *different*
        container. With ``max_files == 1`` a put that protected only the entry
        it was inserting would evict that newly-active container out of the DOM
        while ``self.active`` still pointed at it — a detached-active blank pane
        that never self-heals. Callers therefore pass ``protect=<active>`` so the
        active preview is never the one evicted (the stale/incoming container is
        evicted instead, keeping the cache at ``max_files``)."""
        if container.total_chunks < self.min_chunks:
            return []
        key = (container.parent_doc_id, container.query_signature)
        self._cache[key] = container
        self._cache.move_to_end(key)
        evicted: list[PreviewContainer] = []
        while len(self._cache) > self.max_files:
            for k, old in self._cache.items():
                if old is protect:
                    continue
                evicted.append(old)
                del self._cache[k]
                break
            else:
                break
        return evicted

    def clear(self) -> list[PreviewContainer]:
        """Drop everything; return the previously-cached containers so
        the caller can remove them from the DOM."""
        evicted = list(self._cache.values())
        self._cache.clear()
        return evicted
