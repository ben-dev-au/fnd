"""Preview must scroll to the first match — flat (pdf/txt) and structural (md)."""

from __future__ import annotations

from pathlib import Path

import pytest
from textual.containers import VerticalScroll
from textual.widgets import Tree

from fnd.index import build_index
from fnd.tui import FNDApp
from fnd.tui.line_buffer import LineBufferPreview
from tests._pilot_wait import safe_pause, settle, wait_until


@pytest.fixture
def built_index(fixtures_dir: Path, tmp_index_dir: Path) -> Path:
    build_index(roots=[fixtures_dir], index_dir=tmp_index_dir, collection="default")
    return tmp_index_dir


@pytest.mark.asyncio
async def test_flat_preview_scrolls_to_match_on_initial_query(built_index: Path) -> None:
    app = FNDApp(index_dir=built_index, initial_query="blue penguin sandwich")
    async with app.run_test(size=(120, 40)) as pilot:
        await wait_until(
            pilot,
            lambda: (
                bool(app._groups)
                and bool(list(app.query(LineBufferPreview)))
                and next(iter(app.query(LineBufferPreview))).scroll_y > 0
            ),
            timeout=15.0,
            message="flat preview never scrolled to match",
        )
        buf = next(iter(app.query(LineBufferPreview)))
        assert buf.scroll_y > 0


@pytest.mark.asyncio
async def test_flat_preview_scrolls_after_second_query(built_index: Path) -> None:
    app = FNDApp(index_dir=built_index, initial_query="introduction")
    async with app.run_test(size=(120, 40)) as pilot:
        await wait_until(
            pilot,
            lambda: app._active_flat_buffer is not None and app._active_flat_buffer._fv is not None,
            timeout=15.0,
            message="initial flat buffer never activated",
        )
        # Snapshot the FileView the first query installed. ``_run_query``
        # below either swaps in a new FileView (different match lines)
        # or clears it; either way ``_fv`` identity changes. Without
        # this token the predicate can match the first query's already-
        # scrolled buffer before the second query has rewired it.
        pre_fv = app._active_flat_buffer._fv  # type: ignore[union-attr]
        app._run_query("blue penguin sandwich")
        await wait_until(
            pilot,
            lambda: (
                app._active_flat_buffer is not None
                and app._active_flat_buffer._fv is not pre_fv
                and (app._active_flat_buffer.scroll_y > 0 or not app._active_flat_buffer._fv)
            ),
            timeout=15.0,
            message="flat buffer never settled after second query",
        )
        active = app._active_flat_buffer
        assert active is not None
        assert active.scroll_y > 0 or not active._fv


@pytest.mark.asyncio
async def test_md_preview_scrolls_to_match_chunk(tmp_path: Path, tmp_index_dir: Path) -> None:
    notes = tmp_path / "notes"
    notes.mkdir()
    lines = ["# Top heading", "Some lead-in text.", ""]
    for i in range(60):
        lines.extend([f"## Section {i}", f"Section {i} body.", ""])
    lines.extend(["## Late section", "Here is the unicorn-anchor mention."])
    (notes / "big.md").write_text("\n".join(lines), encoding="utf-8")
    build_index(roots=[notes], index_dir=tmp_index_dir, collection="notes")

    app = FNDApp(index_dir=tmp_index_dir, initial_query="unicorn-anchor")
    async with app.run_test(size=(120, 40)) as pilot:
        pane = app.query_one("#preview_pane", VerticalScroll)
        await wait_until(
            pilot,
            lambda: pane.scroll_y > 0,
            timeout=15.0,
            message=f"preview never scrolled; scroll_y={pane.scroll_y}",
        )
        assert app._groups
        assert app._groups[0].hits[0].chunk_seq > 0
        assert pane.scroll_y > 0, f"scroll_y={pane.scroll_y}"


@pytest.mark.asyncio
async def test_md_preview_scrolls_when_match_is_in_first_chunk(
    tmp_path: Path, tmp_index_dir: Path
) -> None:
    """User's exact symptom: chunk_seq=0 with the match many paragraphs in.
    Scrolling to the chunk widget (which IS at file top) was the bug;
    scrolling to first_match_block lands on the matched paragraph."""
    notes = tmp_path / "notes"
    notes.mkdir()
    body = ["# Sample Notes v2", ""]
    for i in range(40):
        body.extend([f"Intro paragraph {i}.", ""])
    body.extend(["And then the compromise paragraph appears here.", ""])
    for i in range(20):
        body.extend([f"Trailing paragraph {i}.", ""])
    (notes / "sample.md").write_text("\n".join(body), encoding="utf-8")
    build_index(roots=[notes], index_dir=tmp_index_dir, collection="notes")

    app = FNDApp(index_dir=tmp_index_dir, initial_query="compromise")
    async with app.run_test(size=(120, 40)) as pilot:
        pane = app.query_one("#preview_pane", VerticalScroll)
        await wait_until(
            pilot,
            lambda: pane.scroll_y > 0,
            timeout=15.0,
            message=f"preview never scrolled; scroll_y={pane.scroll_y}",
        )
        assert pane.scroll_y > 0, f"scroll_y={pane.scroll_y}"


@pytest.mark.asyncio
async def test_navigating_down_results_scrolls_each_preview(
    tmp_path: Path, tmp_index_dir: Path
) -> None:
    notes = tmp_path / "notes"
    notes.mkdir()
    for label, suffix in [("alpha", "a"), ("beta", "b"), ("gamma", "c")]:
        lines = ["# Top heading", "Lead-in text.", ""]
        for i in range(40):
            lines.extend([f"## Section {i}", f"Filler text in section {i}.", ""])
        lines.extend(["## Anchor section", f"Here is unicorn-anchor-{suffix} in {label}."])
        (notes / f"{label}.md").write_text("\n".join(lines), encoding="utf-8")
    build_index(roots=[notes], index_dir=tmp_index_dir, collection="notes")

    app = FNDApp(
        index_dir=tmp_index_dir,
        initial_query="unicorn-anchor-a unicorn-anchor-b unicorn-anchor-c",
    )
    async with app.run_test(size=(120, 40)) as pilot:
        pane = app.query_one("#preview_pane", VerticalScroll)
        rtree = app.query_one("#results_pane", Tree)
        await wait_until(
            pilot,
            lambda: len(app._groups) >= 2,
            timeout=15.0,
            message="results never accumulated 2 groups",
        )
        for i, _g in enumerate(app._groups):
            expected_parent = app._groups[i].parent_id
            rtree.focus()
            await safe_pause(pilot)
            rtree.cursor_line = rtree.cursor_line + 1 if i > 0 else 1
            # Each file switch swaps in a new PreviewContainer (scroll_y
            # resets to 0 mid-mount). Predicate must bind to THIS
            # iteration's container, otherwise the leftover scroll_y
            # from the previous file passes the check before the swap
            # has landed.
            await wait_until(
                pilot,
                lambda parent=expected_parent: (
                    app._active_preview is not None
                    and app._active_preview.parent_doc_id == parent
                    and pane.scroll_y > 0
                ),
                timeout=20.0,
                message=(
                    f"result {i} parent={expected_parent} "
                    f"active={app._active_preview.parent_doc_id if app._active_preview else None} "
                    f"scroll_y={pane.scroll_y}"
                ),
            )


@pytest.mark.asyncio
async def test_md_preview_scrolls_when_first_match_is_in_a_table(
    tmp_path: Path, tmp_index_dir: Path
) -> None:
    """Table cells (FNDMarkdownTH/TD) have zero region because the
    parent MarkdownTable paints as a single Rich renderable. When the
    first match lands inside a table the scroll must fall back to the
    chunk widget, not no-op against the zero-region cell."""
    notes = tmp_path / "notes"
    notes.mkdir()
    body = ["# Top", "Intro paragraph.", ""]
    for i in range(40):
        body.extend([f"Filler paragraph {i}.", ""])
    body.extend(
        [
            "## A section with a table",
            "",
            "| Attack | Notes |",
            "| ------ | ----- |",
            "| Phishing | Attackers compromise users via fake portals. |",
            "| Malware  | Targets endpoints. |",
            "",
            "Tail paragraph.",
        ]
    )
    (notes / "tables.md").write_text("\n".join(body), encoding="utf-8")
    build_index(roots=[notes], index_dir=tmp_index_dir, collection="notes")

    app = FNDApp(index_dir=tmp_index_dir, initial_query="compromise")
    async with app.run_test(size=(120, 40)) as pilot:
        pane = app.query_one("#preview_pane", VerticalScroll)
        await wait_until(
            pilot,
            lambda: pane.scroll_y > 0,
            timeout=15.0,
            message=f"preview never scrolled; scroll_y={pane.scroll_y}",
        )
        assert pane.scroll_y > 0, f"scroll_y={pane.scroll_y}"


@pytest.mark.asyncio
async def test_md_preview_scrolls_to_matched_row_inside_tall_table(
    tmp_path: Path, tmp_index_dir: Path
) -> None:
    """Regression: a match in a LOWER row of a tall table must scroll the
    matched row into view — not stop at the top of the table.

    The W3 table renders every row in one full-height DataTable (a single
    Rich render, no per-cell widgets, no internal scroll), so scrolling to
    the table widget only reaches its top and leaves a lower-row match
    off-screen. ``scroll_y > 0`` is NOT a sufficient assertion here: the
    top-of-table bug satisfies it.

    Two distinct defects are guarded:

    1. The matched coordinate must be the cell that actually contains the
       query term — not the first cell carrying *any* Content span. Early
       rows here use inline ``code`` / **bold** styling (as a real
       flashcards table does); the buggy coord logic picked the first
       styled cell (near the top) instead of the matched row below.
    2. The pane must scroll so that matched cell lands on-screen.

    We assert both: the cell at the recorded coordinate contains the query
    term, and its on-screen position is inside the preview viewport."""
    from textual.widgets import DataTable

    from fnd.tui.app import FNDMarkdownTableDT

    notes = tmp_path / "notes"
    notes.mkdir()
    lines = ["# Notes", "", "Intro.", "", "| Term | Definition |", "| --- | --- |"]
    # Early rows carry markdown styling (inline code, bold) so their cell
    # Content gains styling spans — the trap the old coord logic fell into.
    for i in range(40):
        lines.append(f"| Term{i} | Use `func{i}()` and **bold{i}** in definition {i}. |")
    lines.append("| Determinism | A Deterministic system always gives the same output. |")
    for i in range(40, 50):
        lines.append(f"| Term{i} | Trailing `code{i}` definition {i}. |")
    (notes / "tall_table.md").write_text("\n".join(lines), encoding="utf-8")
    build_index(roots=[notes], index_dir=tmp_index_dir, collection="notes")

    def matched_dt() -> DataTable[object] | None:
        for wrapper in app.query(FNDMarkdownTableDT):
            for dt in wrapper.query(DataTable):
                if getattr(dt, "_fnd_match_coord", None) is not None and dt.region.height > 0:
                    return dt
        return None

    app = FNDApp(index_dir=tmp_index_dir, initial_query="Deterministic")
    async with app.run_test(size=(120, 40)) as pilot:
        pane = app.query_one("#preview_pane", VerticalScroll)
        await wait_until(
            pilot,
            lambda: matched_dt() is not None and pane.scroll_y > 0,
            timeout=15.0,
            message="table preview never scrolled / no DataTable match coord",
        )
        await settle(pilot)
        dt = matched_dt()
        assert dt is not None, "matched DataTable never laid out"
        coord = dt._fnd_match_coord  # type: ignore[attr-defined]

        # Defect 1: the coordinate points at the cell that contains the match.
        cell_value = dt.get_cell_at(coord)
        cell_text = getattr(cell_value, "plain", str(cell_value))
        assert "Deterministic" in cell_text, (
            f"match coord {coord} points at {cell_text!r}, not the cell "
            f"containing the query term — coord resolved to a merely-styled cell"
        )

        # Defect 2: that cell is scrolled on-screen.
        cell_region = dt._get_cell_region(coord)  # type: ignore[attr-defined]
        csy = dt.region.y + cell_region.y - int(dt.scroll_offset.y)
        top, bottom = pane.region.y, pane.region.y + pane.region.height
        assert top <= csy < bottom, (
            f"matched table cell at screen y={csy} is outside the preview "
            f"viewport [{top}, {bottom}) — scrolled to the top of the table, "
            f"not to the matched row (pane.scroll_y={pane.scroll_y})"
        )


@pytest.mark.asyncio
async def test_md_scroll_with_varied_constructs(tmp_path: Path, tmp_index_dir: Path) -> None:
    """Frontmatter, headings, lists, tables, code blocks, blockquotes —
    the match must still find its block."""
    notes = tmp_path / "notes"
    notes.mkdir()
    body = """---
title: Sample Notes v2
tags: [security, sample]
---

# Sample Notes v2

## Overview

Lead-in paragraph.

- Recap one
- Recap two
- Recap three

> A quote from the textbook.

```python
def safe():
    return True
```

| Col A | Col B |
| ----- | ----- |
| x     | y     |

## Module 1 — Topic 3 Cybersecurity Attacks

Some intro text.

1. First numbered point
2. Second numbered point
3. An attacker can compromise the system via spear-phishing.
4. Fourth numbered point

## Conclusion

Wrap-up paragraph.
"""
    (notes / "sample.md").write_text(body, encoding="utf-8")
    build_index(roots=[notes], index_dir=tmp_index_dir, collection="notes")

    app = FNDApp(index_dir=tmp_index_dir, initial_query="compromise")
    async with app.run_test(size=(120, 40)) as pilot:
        pane = app.query_one("#preview_pane", VerticalScroll)
        await wait_until(
            pilot,
            lambda: pane.scroll_y > 0,
            timeout=15.0,
            message=f"preview never scrolled; scroll_y={pane.scroll_y}",
        )
        assert pane.scroll_y > 0, f"scroll_y={pane.scroll_y}"


@pytest.mark.asyncio
async def test_flat_preview_no_jump_on_install(tmp_path: Path, tmp_index_dir: Path) -> None:
    """Flat buffer must already be scrolled to the match before first paint
    — no flash to file top + jump-to-match."""
    notes = tmp_path / "notes"
    notes.mkdir()
    lines = [f"filler line {i}" for i in range(200)]
    lines.append("This is a unicorn-anchor mention deep in the file.")
    lines += [f"trailing line {i}" for i in range(50)]
    (notes / "long.txt").write_text("\n".join(lines), encoding="utf-8")
    build_index(roots=[notes], index_dir=tmp_index_dir, collection="notes")

    app = FNDApp(index_dir=tmp_index_dir, initial_query="unicorn-anchor")
    async with app.run_test(size=(120, 40)) as pilot:
        await wait_until(
            pilot,
            lambda: app._active_flat_buffer is not None and app._active_flat_buffer.scroll_y > 0,
            timeout=15.0,
            message="flat buffer never scrolled",
        )
        active = app._active_flat_buffer
        assert active is not None, "no active flat buffer"
        assert active.scroll_y > 0, (
            f"buffer revealed at scroll_y=0; virtual_size={active.virtual_size}"
        )


@pytest.mark.asyncio
async def test_flat_match_lands_a_quarter_down_not_at_top(
    tmp_path: Path, tmp_index_dir: Path
) -> None:
    """Flat (PDF/TXT) match drops ~25% down the viewport — context above it,
    consistent with the structural preview — not pinned to the top row.
    ``scroll_y > 0`` alone can't catch a regression to top-anchoring; assert
    the match's on-screen row equals the context margin."""
    notes = tmp_path / "notes"
    notes.mkdir()
    lines = [f"filler line {i}" for i in range(200)]
    lines.append("This is a quartzfin mention deep in the file.")
    lines += [f"trailing line {i}" for i in range(50)]
    (notes / "long.txt").write_text("\n".join(lines), encoding="utf-8")
    build_index(roots=[notes], index_dir=tmp_index_dir, collection="notes")

    app = FNDApp(index_dir=tmp_index_dir, initial_query="quartzfin")
    async with app.run_test(size=(120, 40)) as pilot:
        await wait_until(
            pilot,
            lambda: app._active_flat_buffer is not None and app._active_flat_buffer.scroll_y > 0,
            timeout=15.0,
            message="flat buffer never scrolled",
        )
        await settle(pilot)
        buf = app._active_flat_buffer
        assert buf is not None
        assert buf._fv is not None
        match_logical = min(buf._fv.first_hit_line_in_chunk.values())
        match_visual = buf._logical_to_visual_y(match_logical)
        on_screen_row = match_visual - int(buf.scroll_offset.y)
        margin = int(buf.size.height * 0.25)
        assert on_screen_row == margin, (
            f"flat match at on-screen row {on_screen_row}, expected {margin} (~25% down); "
            f"scroll_y={buf.scroll_offset.y} match_visual={match_visual}"
        )
