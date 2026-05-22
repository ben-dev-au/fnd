"""Preview must scroll to the first match — flat (pdf/txt) and structural (md)."""

from __future__ import annotations

from pathlib import Path

import pytest
from textual.containers import VerticalScroll
from textual.widgets import Tree

from fnd.index import build_index
from fnd.tui import FNDApp
from fnd.tui.line_buffer import LineBufferPreview
from tests._pilot_wait import safe_pause, wait_until


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
            lambda: app._active_flat_buffer is not None,
            timeout=15.0,
            message="initial flat buffer never activated",
        )
        app._run_query("blue penguin sandwich")
        await wait_until(
            pilot,
            lambda: (
                app._active_flat_buffer is not None
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
    body = ["# SFO Wk3 Notes v2", ""]
    for i in range(40):
        body.extend([f"Intro paragraph {i}.", ""])
    body.extend(["And then the compromise paragraph appears here.", ""])
    for i in range(20):
        body.extend([f"Trailing paragraph {i}.", ""])
    (notes / "sfo.md").write_text("\n".join(body), encoding="utf-8")
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
            rtree.focus()
            await safe_pause(pilot)
            rtree.cursor_line = rtree.cursor_line + 1 if i > 0 else 1
            # Each file switch resets scroll_y to 0 while the new
            # PreviewContainer mounts; wait for the end-of-mount
            # re-anchor to fire.
            await wait_until(
                pilot,
                lambda: pane.scroll_y > 0,
                timeout=20.0,
                message=f"result {i} scroll_y={pane.scroll_y}",
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
async def test_md_scroll_with_varied_constructs(tmp_path: Path, tmp_index_dir: Path) -> None:
    """Frontmatter, headings, lists, tables, code blocks, blockquotes —
    the match must still find its block."""
    notes = tmp_path / "notes"
    notes.mkdir()
    body = """---
title: SFO Wk3 Notes v2
tags: [security, sfo]
---

# SFO Wk3 Notes v2

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
    (notes / "sfo.md").write_text(body, encoding="utf-8")
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
        assert (
            active.scroll_y > 0
        ), f"buffer revealed at scroll_y=0; virtual_size={active.virtual_size}"
