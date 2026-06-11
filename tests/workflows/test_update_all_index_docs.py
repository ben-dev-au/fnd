"""Update all collections — verify docs land in the Tantivy index.

Catches the bug where the chain visibly walks every collection (and the
modal renders "Done.") but the LAST collection's docs are missing from
the index. The existing test_update_all suite only checks that
``start_indexer`` fires once per collection in queue order. That assert
is satisfied even when the final task is launched but immediately
torn down before its writer commits.

This test waits for the chain to complete, then opens the Tantivy
index and counts docs per collection. Every collection in the queue
must have at least one doc.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import tantivy

from fnd.config import CollectionConfig, Config, Defaults, SourceConfig
from fnd.schema import F_COLLECTION, build_schema
from fnd.tui import FNDApp

from .conftest import wait_until


def _per_collection_doc_counts(index_dir: Path, names: list[str]) -> dict[str, int]:
    schema = build_schema()
    idx = tantivy.Index(schema, path=str(index_dir))
    idx.reload()
    s = idx.searcher()
    counts: dict[str, int] = {}
    for n in names:
        q = tantivy.Query.term_query(schema, F_COLLECTION, n)
        result = s.search(q, limit=1, count=True)
        # tantivy's SearchResult exposes a ``count`` attribute when
        # ``count=True`` was passed; the type stubs do not surface it.
        counts[n] = int(getattr(result, "count", 0))
    return counts


@pytest.mark.asyncio
async def test_chain_commits_final_collection_docs(tmp_path: Path) -> None:
    """Four-collection chain must leave docs for every collection —
    including the final one — in the index after the chain completes."""
    names = ["alpha", "beta", "gamma", "delta"]
    # Distinct corpora per collection so parent_id-based delete-then-
    # re-add does not let each chain step wipe the prior step's docs
    # (that interaction is a separate bug from the one we are testing).
    collections = {}
    for n in names:
        root = tmp_path / f"corpus_{n}"
        root.mkdir()
        (root / f"{n}_doc.md").write_text(f"# {n}\n\nbody text for {n} indexing.\n")
        collections[n] = CollectionConfig(sources=[SourceConfig(path=root)])
    cfg = Config(defaults=Defaults(), collections=collections)
    index_dir = tmp_path / "index"
    app = FNDApp(index_dir=index_dir, config=cfg)

    from fnd.tui.first_reindex_warning import mark_seen as _mark_seen
    from fnd.tui.settings_screen import UpdateAllConfirm

    # Skip the warning modal so the chain runs without interactive
    # confirmation each step.
    _mark_seen()

    invocations: list[str] = []
    original_start = app.start_indexer

    def _record(*, collection: str, **kw: object) -> bool:
        invocations.append(collection)
        return original_start(collection=collection, **kw)  # type: ignore[arg-type]

    app.start_indexer = _record  # type: ignore[method-assign]

    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.push_screen(UpdateAllConfirm(collection_names=names))
        await pilot.pause()
        await pilot.press("enter")
        # First, wait until every collection has been launched.
        ok = await wait_until(
            pilot,
            lambda: len(invocations) >= len(names),
            timeout=20.0,
            ticks=200,
        )
        assert ok, f"chain never launched every collection: {invocations}"
        # Then wait for the chain to drain: last task completed and
        # bookkeeping reset.
        ok = await wait_until(
            pilot,
            lambda: (
                app._indexer.chain_remaining == []
                and not app._indexer.chain_callback_pending
                and (app._indexer.task is None or app._indexer.task.done())
            ),
            timeout=20.0,
            ticks=200,
        )
        assert ok, (
            f"chain never reset: remaining={app._indexer.chain_remaining}, "
            f"callback_pending={app._indexer.chain_callback_pending}, "
            f"task_done={app._indexer.task and app._indexer.task.done()}"
        )
        # Let any in-flight wait_merging_threads / file syncs settle so
        # the searcher we open below sees the committed segments.
        for _ in range(8):
            await asyncio.sleep(0)
            await pilot.pause()

    counts = _per_collection_doc_counts(index_dir, names)
    missing = [n for n, c in counts.items() if c == 0]
    assert not missing, f"chain finished but no docs for: {missing}; full counts={counts}"
