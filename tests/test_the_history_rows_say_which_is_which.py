"""Inside the run history, the indexing and texturising rows were identical in
shape and unlabelled.

    └ ▼ research-notes
      ├ 0 new · 312 already
      └ 0 new · 1 already

The second reads as another collection, or as a failure count. It is neither.
`compact=True` dropped the heading to survive a clip, and the heading is the
only thing telling the two rows apart.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from textual.widgets import Tree

from fnd.tui import FNDApp
from fnd.tui.indexer_modal import ChainStepSummary, IndexerScreen


def _summary() -> ChainStepSummary:
    return ChainStepSummary(
        collection="research-notes",
        files_total=312,
        pdfs_total=1,
        indexed_newly=0,
        indexed_already=312,
        textured_newly=0,
        textured_already=1,
        still_flat=0,
        failed=0,
        removed=0,
        elapsed_s=1.0,
    )


async def _rows(app: FNDApp, pilot: Any) -> list[str]:
    app._indexer.chain_history = [_summary()]
    app.push_screen(IndexerScreen("research-notes"))
    for _ in range(20):
        await pilot.pause()
    screen = app.screen
    assert isinstance(screen, IndexerScreen)
    screen._refresh_history_band()
    tree = screen.query_one("#indexer_history_tree", Tree)
    tree.root.expand()
    for node in tree.root.children:
        node.expand()
    for _ in range(10):
        await pilot.pause()
    return ["".join(s.text for s in strip) for strip in screen._compositor.render_strips()]


@pytest.mark.asyncio
@pytest.mark.parametrize("width", [62, 110])
async def test_the_two_history_rows_are_tellable_apart(width: int, tmp_index_dir: Path) -> None:
    """Same shape, different meaning: the heading is what separates them."""
    app = FNDApp(index_dir=tmp_index_dir)
    async with app.run_test(size=(width, 34)) as pilot:
        await pilot.pause()
        rows = await _rows(app, pilot)

    counted = [r for r in rows if "new" in r and ("Files" in r or "PDFs" in r)]
    assert counted, "nothing painted"
    indexed = [r for r in counted if "Files" in r]
    textured = [r for r in counted if "PDFs" in r]
    assert indexed, counted
    assert textured, counted
    assert indexed != textured, counted
