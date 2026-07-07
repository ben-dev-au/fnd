"""Regression: the preview must never activate a container detached from the DOM.

Data-reproduced root cause of the "preview blank until I select another result
and come back" strand: under rapid navigation the single-slot widget cache plus
concurrent prefetch/mount finallys can leave a container in the cache after it
has been removed from the tree. The warm / resume path then activated + scrolled
that detached ghost and revealed a blank pane that only healed on re-navigation.

``dispatch_mount`` now treats a detached cache entry as a miss: it purges the
entry and builds a fresh, attached container instead. That purge is synchronous
(it happens before the async mount is even spawned), so these tests pin it
deterministically rather than racing the run_test mount pump.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fnd.index import build_index
from fnd.query import FileChunk, FileGroup
from fnd.tui import FNDApp
from fnd.tui.preview_dispatcher import choose_preview_mode
from fnd.tui.widgets.preview_container import PreviewContainer
from tests._pilot_wait import safe_pause


@pytest.fixture
def built_index(tmp_path: Path, tmp_index_dir: Path) -> Path:
    # A small MARKDOWN corpus so results render on the structural (per-chunk
    # widget) path the detached-container guards protect — the flat PDF/TXT path
    # has no widget cache and no fast path.
    root = tmp_path / "corpus"
    root.mkdir()
    for i in range(6):
        (root / f"note_{i:02d}.md").write_text(
            f"# Apples and oranges {i}\n\n"
            "This note is about apples and oranges for query matching. "
            f"Section {i} discusses the apple in detail.\n\n"
            "## More\n\nAnother apple paragraph here.\n"
        )
    build_index(roots=[root], index_dir=tmp_index_dir, collection="default")
    return tmp_index_dir


def _structural_group(app: FNDApp) -> tuple[FileGroup, list[FileChunk]]:
    """First result whose chunks render on the STRUCTURAL (per-chunk widget)
    path — the flat PDF/TXT path has no widget cache and no fast path, so the
    detached-container guards under test don't apply there."""
    assert app._search.searcher is not None
    for g in app._search.groups:
        chunks = app._search.searcher.get_file_chunks(g.parent_id)
        if chunks and choose_preview_mode(chunks) != "flat":
            return g, chunks
    pytest.skip("no structural (markdown) result in the fixture corpus")


@pytest.mark.asyncio
async def test_dispatch_purges_a_detached_cache_entry(built_index: Path) -> None:
    """A cached container that has been removed from the DOM is not a valid hit:
    dispatch must drop it (so the warm/resume path can't activate a zero-region
    ghost) rather than serve it as a cache hit."""
    app = FNDApp(index_dir=built_index, initial_query="apple")
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app._search.groups, "setup — query produced no results"
        preview = app._preview
        g, chunks = _structural_group(app)
        query_sig = app._search.query_signature()

        # Poison the cache with a DETACHED container for this file+query — the
        # exact divergence the eviction/sweep race produces — and make the
        # dispatch take the cache-resolution path (no same-file active shortcut).
        ghost = PreviewContainer(
            parent_doc_id=g.parent_id,
            query_signature=query_sig,
            total_chunks=len(chunks),
        )
        preview.active = None
        preview.chunk_cache[g.parent_id] = chunks
        preview.preview_cache.put(ghost)
        assert ghost.parent is None, "setup — the cached container is detached"
        assert preview.preview_cache.get(g.parent_id, query_sig) is ghost

        preview.dispatch_mount(g.parent_id, chunks[0].chunk_seq, chunks)

        # Synchronous guard effect: the detached entry is gone and was never made
        # the active preview. (A fresh, attached container is built asynchronously
        # in its place — see the harness runs for the end-to-end no-blank proof.)
        assert preview.preview_cache.get(g.parent_id, query_sig) is not ghost, (
            "the detached cache entry must be purged, not served as a hit"
        )
        assert preview.active is not ghost, "the detached container must never be activated"

        # Let the fresh mount settle; the resulting active preview is attached.
        await safe_pause(pilot)
        await pilot.pause()
        active = preview.active
        assert active is not ghost
        if active is not None:
            assert active.parent is not None, "the rebuilt active preview must be attached"


@pytest.mark.asyncio
async def test_dispatch_skips_the_already_active_fast_path_when_active_is_detached(
    built_index: Path,
) -> None:
    """The 'same file already active' fast path scrolls ``self.active`` in place.
    If a race detached that active container, taking the fast path would scroll a
    zero-region ghost and strand a blank pane — so dispatch must NOT enter it.
    A spy on the instant-scroll entry point proves the fast path is skipped."""
    app = FNDApp(index_dir=built_index, initial_query="apple")
    async with app.run_test() as pilot:
        await pilot.pause()
        preview = app._preview
        g, chunks = _structural_group(app)
        query_sig = app._search.query_signature()

        # A "complete" active container for this file so the fast path's own
        # gate (is_complete) would pass — then detach it out from under active.
        detached = PreviewContainer(
            parent_doc_id=g.parent_id,
            query_signature=query_sig,
            total_chunks=len(chunks),
        )
        detached.mounted_indices = set(range(len(chunks)))  # is_complete == True
        preview.active = detached
        preview.chunk_cache[g.parent_id] = chunks
        assert detached.parent is None, "setup — active container is detached"
        assert detached.is_complete, "setup — active container would pass the fast-path gate"

        scrolled: list[object] = []
        orig = preview._settled_instant_scroll

        async def _spy(container: object, parent_id: str, focus_chunk_seq: int) -> None:
            scrolled.append(container)
            await orig(container, parent_id, focus_chunk_seq)  # type: ignore[arg-type]

        preview._settled_instant_scroll = _spy  # type: ignore[assignment,method-assign]

        preview.dispatch_mount(g.parent_id, chunks[0].chunk_seq, chunks)

        assert detached not in scrolled, (
            "a detached active container must not be scrolled in place — the "
            "already-active fast path must fall through to a fresh rebuild"
        )
        assert preview.active is not detached, "the detached container must be dropped as active"
        await safe_pause(pilot)
