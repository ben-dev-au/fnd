"""At 80 columns the removed count keeps its word, not a bare digit.

`Indexed: 0 newly indexed  0 already indexed  2` cut the word off, with two
blank cells beside it and no ellipsis. The count matters: the run that printed
it had emptied the index (Files in index went 9 → 0 → 9).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from fnd.tui import FNDApp
from fnd.tui.indexer_modal import IndexerScreen


async def _indexed_line(app: FNDApp, pilot: Any, *, removed: int, failed: int = 0) -> str:
    app.push_screen(IndexerScreen("notes"))
    for _ in range(15):
        await pilot.pause()
    screen = app.screen
    assert isinstance(screen, IndexerScreen)
    screen._update_status_lines(
        pdfs_total=0,
        indexed_newly=12,
        indexed_already=340,
        textured_newly=0,
        textured_already=0,
        still_flat=0,
        failed=failed,
        removed=removed,
    )
    for _ in range(5):
        await pilot.pause()
    rows = ["".join(s.text for s in strip) for strip in screen._compositor.render_strips()]
    # The row itself, not the block: a count split across two rows still
    # paints a bare digit at the end of the first, which is the defect.
    return next((r for r in rows if "Indexed:" in r), "")


@pytest.mark.asyncio
@pytest.mark.parametrize("width", [60, 80, 110])
async def test_the_removed_count_keeps_its_noun(width: int, tmp_index_dir: Path) -> None:
    app = FNDApp(index_dir=tmp_index_dir)
    async with app.run_test(size=(width, 30)) as pilot:
        await pilot.pause()
        line = await _indexed_line(app, pilot, removed=2)

    assert "2 removed" in line, f"at {width} columns: {line.strip()!r}"


@pytest.mark.asyncio
async def test_a_failure_chip_is_never_lost(tmp_index_dir: Path) -> None:
    """Removed AND failed together do not fit one row at 80 columns.

    They wrap rather than clip, which is the property that matters: the
    reported defect was a word disappearing, not a word moving down a line.
    """
    app = FNDApp(index_dir=tmp_index_dir)
    async with app.run_test(size=(80, 30)) as pilot:
        await pilot.pause()
        app.push_screen(IndexerScreen("notes"))
        for _ in range(15):
            await pilot.pause()
        screen = app.screen
        assert isinstance(screen, IndexerScreen)
        screen._update_status_lines(
            pdfs_total=0,
            indexed_newly=12,
            indexed_already=340,
            textured_newly=0,
            textured_already=0,
            still_flat=0,
            failed=1,
            removed=2,
        )
        for _ in range(5):
            await pilot.pause()
        rows = ["".join(s.text for s in strip) for strip in screen._compositor.render_strips()]
        start = next(i for i, r in enumerate(rows) if "Indexed:" in r)
        block = " ".join(rows[start : start + 3])

    # Row boundaries carry the modal border, so the chip's own words are
    # checked rather than the phrase: at 80 with both counts it wraps between
    # "⚠ 1" and "failed".
    assert "2 removed" in block, block
    assert "failed" in block, block


@pytest.mark.asyncio
async def test_an_ordinary_run_still_reads(tmp_index_dir: Path) -> None:
    """The control: the counts that are always there must stay legible."""
    app = FNDApp(index_dir=tmp_index_dir)
    async with app.run_test(size=(80, 30)) as pilot:
        await pilot.pause()
        line = await _indexed_line(app, pilot, removed=0)

    assert "12" in line, line.strip()
    assert "340" in line, line.strip()
    assert "removed" not in line, "a run that removed nothing stays quiet"
