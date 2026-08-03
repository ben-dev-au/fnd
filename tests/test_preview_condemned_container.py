"""Regression: a container whose removal is merely *queued* is already dead.

``Widget.remove()`` is asynchronous. ``App._prune`` posts a ``Prune`` message and
flags ``_pruning``; the widget keeps a live ``.parent`` and still turns up in
``app.query()`` until that message is processed — and when it is, ``on_prune``
closes the widget's message loop and detaches it. So between the two there is a
window where a container looks perfectly healthy but is doomed.

Every previous guard in this subsystem tested ``parent is None`` ("already
detached"), which is blind to that window. The consequence, reproduced by
``dev/tools/preview_blank_fuzz.py``: ``dispatch_mount`` sweeps stranded
containers with ``.remove()`` and then, a few lines later in the same
synchronous block, re-scans the DOM for an adoptable container — and adopts the
very one it just condemned. Because that container still reports a parent, the
mount skips its ``pane.mount()`` and simply builds into it and makes it
``self.active``; the queued ``Prune`` then detaches it. Result: ``active``
points at a widget that is not in the tree, the pane is blank, and nothing
self-heals it (the reveal watchdog only lifts ``-pre-reveal``).

These tests pin the liveness predicate at each seam that consumes it.
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
    assert app._search.searcher is not None
    for g in app._search.groups:
        chunks = app._search.searcher.get_file_chunks(g.parent_id)
        if chunks and choose_preview_mode(chunks) != "flat":
            return g, chunks
    pytest.skip("no structural (markdown) result in the fixture corpus")


async def _quiesce_pane(app: FNDApp, pilot: object) -> None:
    """Drop every container and let the Prune messages actually land, so a test
    starts from a pane with no in-flight removals of its own."""
    app._preview.cancel_mount_task()
    app._preview.cancel_pending_load()
    for w in list(app.query(PreviewContainer)):
        w.remove()
    for _ in range(10):
        await safe_pause(pilot)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_dispatch_never_adopts_a_container_it_just_condemned(built_index: Path) -> None:
    """The stranded-container sweep and the DOM-scan adopt run in the same
    synchronous block. The sweep must not be able to hand the adopt a widget it
    has already condemned — the mount would build into a doomed container and
    strand ``active`` on a detached widget (permanent blank pane)."""
    app = FNDApp(index_dir=built_index, initial_query="apple")
    async with app.run_test() as pilot:
        await safe_pause(pilot)
        assert app._search.groups, "setup — query produced no results"
        preview = app._preview
        g, chunks = _structural_group(app)
        query_sig = app._search.query_signature()

        await _quiesce_pane(app, pilot)

        # Exactly the state a reset (new query / scope clear) leaves behind: a
        # container for this file still in the DOM, but no longer in the cache.
        stray = PreviewContainer(
            parent_doc_id=g.parent_id,
            query_signature=query_sig,
            total_chunks=len(chunks),
        )
        await app.query_one("#preview_pane").mount(stray)
        preview.preview_cache.clear()
        preview.active = None
        preview.chunk_cache[g.parent_id] = chunks
        await safe_pause(pilot)
        assert stray.parent is not None, "setup — stray is mounted"
        assert not stray._pruning, "setup — stray is not condemned"

        mounted_on: list[PreviewContainer] = []
        orig = preview._mount_chunks_async

        def _spy(parent_id, focus, chks, container, **kw):  # type: ignore[no-untyped-def]
            # Sync wrapper: records at coroutine-construction time, i.e. the
            # instant dispatch chose this container.
            mounted_on.append(container)
            return orig(parent_id, focus, chks, container, **kw)

        preview._mount_chunks_async = _spy  # type: ignore[assignment,method-assign]

        preview.dispatch_mount(g.parent_id, chunks[0].chunk_seq, chunks)

        adopted = mounted_on[0] if mounted_on else None
        assert adopted is not None, "dispatch should have started a mount"
        assert not adopted._pruning, (
            "dispatch adopted a container whose Prune is already queued — the "
            "mount will build into a widget that is about to be torn out of the DOM"
        )

        await safe_pause(pilot)
        await safe_pause(pilot)
        active = preview.active
        assert active is not None, "a navigation must leave an active preview"
        assert active.is_attached, "the active preview must be attached to the DOM"
        assert not active._pruning, "the active preview must not be condemned"


@pytest.mark.asyncio
async def test_condemned_cache_entry_is_a_miss(built_index: Path) -> None:
    """A cached container with a queued Prune is not a valid hit. Serving it
    activates a widget that vanishes a tick later."""
    app = FNDApp(index_dir=built_index, initial_query="apple")
    async with app.run_test() as pilot:
        await safe_pause(pilot)
        preview = app._preview
        g, chunks = _structural_group(app)
        query_sig = app._search.query_signature()

        await _quiesce_pane(app, pilot)

        doomed = PreviewContainer(
            parent_doc_id=g.parent_id,
            query_signature=query_sig,
            total_chunks=len(chunks),
        )
        doomed.mounted_indices = set(range(len(chunks)))  # is_complete → warm path
        await app.query_one("#preview_pane").mount(doomed)
        await safe_pause(pilot)
        doomed.remove()  # queued, not yet processed
        assert doomed.parent is not None, "setup — still attached"
        assert doomed._pruning, "setup — removal is queued (condemned)"

        preview.active = None
        preview.chunk_cache[g.parent_id] = chunks
        preview.preview_cache.put(doomed)

        preview.dispatch_mount(g.parent_id, chunks[0].chunk_seq, chunks)

        assert preview.active is not doomed, (
            "a condemned container must never become the active preview"
        )
        assert preview.preview_cache.get(g.parent_id, query_sig) is not doomed, (
            "a condemned cache entry must be purged, not served as a hit"
        )


@pytest.mark.asyncio
async def test_already_active_fast_path_skipped_when_active_is_condemned(
    built_index: Path,
) -> None:
    """The 'same file already active' fast path scrolls ``self.active`` in place
    without remounting. A condemned active container passes the current
    ``parent is not None`` gate, so the fast path would scroll a widget that is
    about to disappear and leave the pane blank."""
    app = FNDApp(index_dir=built_index, initial_query="apple")
    async with app.run_test() as pilot:
        await safe_pause(pilot)
        preview = app._preview
        g, chunks = _structural_group(app)
        query_sig = app._search.query_signature()

        await _quiesce_pane(app, pilot)

        doomed = PreviewContainer(
            parent_doc_id=g.parent_id,
            query_signature=query_sig,
            total_chunks=len(chunks),
        )
        doomed.mounted_indices = set(range(len(chunks)))
        await app.query_one("#preview_pane").mount(doomed)
        await safe_pause(pilot)
        doomed.remove()
        assert doomed.parent is not None, "setup — still attached"
        assert doomed._pruning, "setup — removal is queued (condemned)"
        preview.active = doomed
        preview.chunk_cache[g.parent_id] = chunks

        scrolled: list[object] = []
        orig = preview._settled_instant_scroll

        async def _spy(container: object, parent_id: str, focus_chunk_seq: int) -> None:
            scrolled.append(container)
            await orig(container, parent_id, focus_chunk_seq)  # type: ignore[arg-type]

        preview._settled_instant_scroll = _spy  # type: ignore[assignment,method-assign]

        preview.dispatch_mount(g.parent_id, chunks[0].chunk_seq, chunks)

        assert doomed not in scrolled, "a condemned active container must not be scrolled in place"
        assert preview.active is not doomed


@pytest.mark.asyncio
async def test_mount_finally_does_not_cache_a_condemned_container(built_index: Path) -> None:
    """The mount's ``finally`` refuses to cache a *detached* container. A
    condemned one is equally dead — caching it hands the next visit a hit on a
    widget that is being torn down."""
    import asyncio
    import contextlib

    app = FNDApp(index_dir=built_index, initial_query="apple")
    async with app.run_test() as pilot:
        await safe_pause(pilot)
        preview = app._preview
        g, chunks = _structural_group(app)
        query_sig = app._search.query_signature()

        await _quiesce_pane(app, pilot)
        preview.preview_cache.clear()
        preview.active = None

        # Park the mount in its early-await window so it is still in flight when
        # we condemn the container.
        gate = asyncio.Event()

        async def _blocking_cancel_task_on(_c: object) -> None:
            await gate.wait()

        app._prefetch.cancel_task_on = _blocking_cancel_task_on  # type: ignore[assignment]

        container = PreviewContainer(
            parent_doc_id=g.parent_id,
            query_signature=query_sig,
            total_chunks=len(chunks),
        )
        task = asyncio.create_task(
            preview._mount_chunks_async(
                g.parent_id,
                chunks[0].chunk_seq,
                chunks,
                container,
                reset_generation=preview.reset_generation,
            )
        )
        preview.mount_task = task
        await safe_pause(pilot)
        assert container.parent is not None, "setup — parked mount has the container mounted"

        # Condemn it mid-flight, then release + cancel so the finally runs while
        # the Prune is still only queued.
        container.remove()
        assert container.parent is not None, "setup — still attached in flight"
        assert container._pruning, "setup — condemned in flight"
        task.cancel()
        gate.set()
        with contextlib.suppress(asyncio.CancelledError):
            await task

        assert preview.preview_cache.get(g.parent_id, query_sig) is not container, (
            "a condemned container must not be cached by the mount's finally"
        )
        assert preview.active is not container
