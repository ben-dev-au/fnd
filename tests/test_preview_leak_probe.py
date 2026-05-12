"""Regression: bound PreviewContainer DOM growth in #preview_pane.

Before the audit-fix branch:
- ``_PREVIEW_CACHE_MIN_CHUNKS = 30`` meant short files (most markdown)
  never made it into the LRU cache.
- Uncached containers were ``-hidden``-classed on file switch but never
  removed from the DOM.
- Result: every file visit added a container to ``#preview_pane`` and
  ``_activate_preview_container`` paid an O(N) walk over the growing
  stack — a real linear slowdown for sessions of any length.

After the fix:
- ``_PREVIEW_CACHE_MIN_CHUNKS = 1`` so every complete file is bounded
  by the 8-entry LRU.
- ``_dispatch_preview_mount`` sweeps stranded containers (mid-mount
  cancellations that never reach ``put``).
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def small_corpus(tmp_path: Path) -> Path:
    """Ten short markdown files so each one falls comfortably under
    the pre-fix cache threshold and exercises the LRU eviction path."""
    root = tmp_path / "corpus"
    root.mkdir()
    for i in range(10):
        (root / f"doc_{i:02d}.md").write_text(
            f"# Title {i}\n\nThis is a short note about apples and oranges. "
            f"It contains apple references for query matching. Document {i}.\n"
        )
    return root


@pytest.mark.asyncio
async def test_preview_container_count_bounded_by_cache(
    small_corpus: Path, tmp_index_dir: Path
) -> None:
    """Visiting more files than the LRU capacity must NOT grow the DOM
    unboundedly. With 10 small markdown files visited in sequence, the
    PreviewContainer count must stay at or below
    ``_PREVIEW_CACHE_MAX_FILES``. Pre-fix this grew linearly with visits."""
    from textual.widgets import Tree

    from acorn.index import build_index
    from acorn.tui import AcornApp
    from acorn.tui.app import _PREVIEW_CACHE_MAX_FILES, PreviewContainer

    build_index(roots=[small_corpus], index_dir=tmp_index_dir, collection="default")
    app = AcornApp(index_dir=tmp_index_dir, initial_query="apple")
    async with app.run_test() as pilot:
        await pilot.pause()
        tree = app.query_one("#results_pane", Tree)
        for _ in range(20):
            await pilot.pause()
            if len(tree.root.children) > 0:
                break
        results = list(tree.root.children)
        assert len(results) >= _PREVIEW_CACHE_MAX_FILES + 1, (
            f"Need more than LRU-capacity ({_PREVIEW_CACHE_MAX_FILES}) "
            f"results to exercise eviction; got {len(results)}"
        )
        counts: list[int] = []
        for node in results[: _PREVIEW_CACHE_MAX_FILES + 2]:
            tree.cursor_line = node.line
            await pilot.pause()
            tree.post_message(Tree.NodeSelected(node))
            for _ in range(8):
                await pilot.pause()
            counts.append(len(list(app.query(PreviewContainer))))
        assert max(counts) <= _PREVIEW_CACHE_MAX_FILES, (
            f"PreviewContainer count peaked at {max(counts)} (sequence "
            f"{counts}) but the LRU cap is {_PREVIEW_CACHE_MAX_FILES}. "
            f"Containers are not being removed when their cache slot is "
            f"evicted, leaking widgets into #preview_pane."
        )
