"""Characterization net for preview scroll-to-match.

Pins the observable scroll behaviour of the centralised scroll controller.
Each test asserts the visible outcome (match on-screen / scroll position),
mirroring ``tests/test_preview_scrolls_to_match.py``. The cold file-node
navigation case captured a known off-screen bug; the controller fixes it, so
it is now a hard-asserting regression test (no longer xfailed).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from textual.widgets import DataTable, Tree

from fnd.config import Config, Defaults, RankingProfileConfig
from fnd.index import build_index
from fnd.tui import FNDApp
from fnd.tui.line_buffer import LineBufferPreview
from tests._pilot_wait import safe_pause, settle, wait_until


@pytest.fixture
def built_index(fixtures_dir: Path, tmp_index_dir: Path) -> Path:
    build_index(roots=[fixtures_dir], index_dir=tmp_index_dir, collection="default")
    return tmp_index_dir


@pytest.mark.asyncio
async def test_initial_query_flat_match_scrolls_into_view(built_index: Path) -> None:
    """Initial-query flat (pdf/txt) match scrolls past file top."""
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
async def test_requery_same_flat_file_lands_on_new_match(built_index: Path) -> None:
    """Re-querying the same flat file lands on the new match."""
    app = FNDApp(index_dir=built_index, initial_query="introduction")
    async with app.run_test(size=(120, 40)) as pilot:
        await wait_until(
            pilot,
            lambda: app._active_flat_buffer is not None and app._active_flat_buffer._fv is not None,
            timeout=15.0,
            message="initial flat buffer never activated",
        )
        # Snapshot the FileView the first query installed; the second
        # query either swaps in a new FileView or clears it. Without
        # this token the predicate can match the first query's already-
        # scrolled buffer before the second has rewired it.
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
async def test_md_match_in_tall_table_lands_on_matched_row(
    tmp_path: Path, tmp_index_dir: Path
) -> None:
    """A match in a lower row of a tall table scrolls that row on-screen —
    not the top of the table. ``scroll_y > 0`` is insufficient: the
    top-of-table bug satisfies it. Asserts both the recorded coordinate
    points at the matched cell AND that cell's screen-y is in the pane
    viewport."""
    from fnd.tui.app import FNDMarkdownTableDT

    notes = tmp_path / "notes"
    notes.mkdir()
    lines = ["# Notes", "", "Intro.", "", "| Term | Definition |", "| --- | --- |"]
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
        pane = app.query_one("#preview_pane")
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

        cell_value = dt.get_cell_at(coord)
        cell_text = getattr(cell_value, "plain", str(cell_value))
        assert "Deterministic" in cell_text, (
            f"match coord {coord} points at {cell_text!r}, not the matched cell"
        )

        cell_region = dt._get_cell_region(coord)  # type: ignore[attr-defined]
        csy = dt.region.y + cell_region.y - int(dt.scroll_offset.y)
        top, bottom = pane.region.y, pane.region.y + pane.region.height
        assert top <= csy < bottom, (
            f"matched table cell at screen y={csy} is outside the preview "
            f"viewport [{top}, {bottom}) (pane.scroll_y={pane.scroll_y})"
        )


@pytest.mark.asyncio
async def test_section_to_section_navigation_scrolls_each_match(
    tmp_path: Path, tmp_index_dir: Path
) -> None:
    """Navigating down the results tree scrolls each file's match into view."""
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
        pane = app.query_one("#preview_pane")
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
            # resets to 0 mid-mount). Bind the predicate to THIS file's
            # container so leftover scroll from the previous file can't
            # pass the check before the swap lands.
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


def _coldnav_file(label: str) -> str:
    """A multi-chunk structural md shaped after the real DPC Wk4 note: an
    early-middle section whose match is a prose line a few rows below its
    heading, preceded by varied content (tables, code) so chunk heights are
    non-trivial. The query term ``quartzfin`` is UNIQUE to that prose line and
    appears in NO heading — so the scroll's match-block resolution is correct;
    only the cold-render scroll *position* is at issue."""
    lines: list[str] = [f"# {label} Notes", "", "Lead-in overview paragraph.", ""]
    # A few tall front sections so the match sits ~10-15% down the file by
    # line count, while staying within the background-fill radius (all
    # above-chunks mounted — this is the under-shoot path, not lazy-mount).
    for s in range(8):
        lines.append(f"## Section {s} overview")
        lines.append("")
        for p in range(8):
            lines.append(f"Paragraph {p} in section {s}: prose at length to add height here words.")
            lines.append("")
        if s % 2 == 0:
            lines += ["| Col A | Col B | Col C |", "| --- | --- | --- |"]
            for r in range(5):
                lines.append(f"| item {s}-{r} | value {s}-{r} | note {s}-{r} with extra words |")
            lines.append("")
        else:
            lines += ["```python", f"def section_{s}():", "    return compute_value()", "```", ""]
    # Match section: heading, subheading, then the prose match a few lines below.
    lines.append("## Smart Pointers")
    lines.append("")
    lines.append("#### What Smart Pointers Solve")
    lines.append("")
    lines.append("They manage lifetimes so cleanup is automatic but quartzfin in scope here today.")
    lines.append("")
    lines.append("More prose follows the match to give the chunk height below it now.")
    lines.append("")
    for s in range(9, 50):
        lines.append(f"## Section {s} overview")
        lines.append("")
        for p in range(3):
            lines.append(f"Paragraph {p} in section {s} at moderate length here for filler.")
            lines.append("")
    return "\n".join(lines)


@pytest.mark.asyncio
async def test_cold_nav_to_prefetched_non_first_file_lands_on_screen(
    tmp_path: Path, tmp_index_dir: Path
) -> None:
    """Cold file-node navigation to a prefetched non-first structural file
    lands the (correctly-resolved) prose match on-screen, ~25% down.

    Regression guard for the cold-nav under-shoot the scroll controller fixes:
    before the armed gate, navigating to a prefetched container could leave the
    match just below the fold (the chunk top parked near the viewport edge
    instead of the match dropped ~25% down) — a mid-settle lazy-mount yanked
    position after the scroll committed. The armed gate suppresses lazy-mount
    for the whole settle, so the match stays put. ``scroll_y > 0`` is NOT
    sufficient — the under-shoot scrolls, just not far enough; we assert the
    matched prose widget sits inside the pane viewport.

    Prefetch must be ON: the autouse conftest fixture pins
    ``preview_prefetch_count=0``; an explicit ``Defaults`` value overrides it.
    """
    notes = tmp_path / "notes"
    notes.mkdir()
    for label in ("Alpha", "Bravo", "Charlie", "Delta"):
        (notes / f"{label}.md").write_text(_coldnav_file(label), encoding="utf-8")
    build_index(roots=[notes], index_dir=tmp_index_dir, collection="notes")

    cfg = Config(
        defaults=Defaults(preview_prefetch_count=5, preview_load_debounce_ms=0),
        ranking={"default": RankingProfileConfig()},
    )
    app = FNDApp(index_dir=tmp_index_dir, config=cfg, collection="notes")
    async with app.run_test(size=(120, 40)) as pilot:
        pane = app.query_one("#preview_pane")
        rtree = app.query_one("#results_pane", Tree)
        app._run_query("quartzfin")
        sig = app._current_query_signature()

        await wait_until(
            pilot,
            lambda: len(app._groups) >= 3,
            timeout=15.0,
            message="results never accumulated 3 groups",
        )
        assert len(app._groups) >= 3
        # Match is early-middle, not chunk 0 and not the last chunk.
        assert app._groups[0].hits[0].chunk_seq > 0, "match should not be in the first chunk"

        # Wait for prefetch to pre-mount the NON-first target file's container,
        # so navigation hits the cold/prefetched-container code path.
        target_group = app._groups[1]
        nudged = False
        for _ in range(240):
            await pilot.pause()
            await asyncio.sleep(0.05)
            cont = app._preview_cache.get(target_group.parent_id, sig)
            if cont is not None and cont.mounted_indices:
                break
            if not nudged and not app._user_mount_in_flight():
                app._prefetch_top_results()
                nudged = True
        prefetched = app._preview_cache.get(target_group.parent_id, sig)
        assert prefetched is not None, f"prefetch never built {target_group.parent_id}"
        assert prefetched.mounted_indices, f"prefetch never pre-mounted {target_group.parent_id}"

        # Navigate the user to the (collapsed, non-first) target file node —
        # closest to the real user action — and drive the cold load.
        target_node = rtree.root.children[1]
        rtree.focus()
        await safe_pause(pilot)
        rtree.move_cursor(target_node)
        focus_seq = target_group.hits[0].chunk_seq

        def _content_match_region():  # type: ignore[no-untyped-def]
            """Region of the widget holding the unique query text — the prose
            match the scroll should land on."""
            ap = app._active_preview
            if ap is None or ap.parent_doc_id != target_group.parent_id:
                return None
            chunk = ap.match_targets.get(focus_seq) or ap.chunk_widgets.get(focus_seq)
            if chunk is None:
                return None
            for w in chunk.query("*"):
                if w is chunk:
                    continue
                plain = getattr(getattr(w, "_content", None), "plain", None)
                if plain and "quartzfin" in plain and w.region.height > 0:
                    return w.region
            return None

        await wait_until(
            pilot,
            lambda: (
                app._active_preview is not None
                and app._active_preview.parent_doc_id == target_group.parent_id
                and pane.scroll_y > 0
                and _content_match_region() is not None
            ),
            timeout=20.0,
            message="cold-nav target never activated / content match never laid out",
        )
        await settle(pilot)

        region = _content_match_region()
        assert region is not None, "content match widget never laid out"
        # The prose match must be inside the pane viewport — scroll_y > 0 alone
        # is not enough; the under-shoot scrolls but stops short of the match.
        top, bottom = pane.region.y, pane.region.y + pane.region.height
        assert top <= region.y < bottom, (
            f"content match at screen y={region.y} is outside the preview "
            f"viewport [{top}, {bottom}) — cold-render scroll under-shot, leaving "
            f"the match below the fold (pane.scroll_y={pane.scroll_y})"
        )


def _reading_doc(tmp_path: Path, tmp_index_dir: Path) -> Path:
    """A multi-section markdown doc, scrollable in the preview. The unique
    term ``quartzfin-anchor`` sits near the end so the auto-load scrolls
    well past the top."""
    notes = tmp_path / "notes"
    notes.mkdir()
    lines = ["# Title", "Introductory paragraph with enough words to wrap a little.", ""]
    for i in range(60):
        lines += [
            f"## Section {i}",
            f"Body text for section {i} long enough to wrap at narrow widths and reflow wider.",
            "",
        ]
    lines += ["## Anchor section", "Here is quartzfin-anchor inside the anchor section prose."]
    (notes / "doc.md").write_text("\n".join(lines), encoding="utf-8")
    build_index(roots=[notes], index_dir=tmp_index_dir, collection="notes")
    return tmp_index_dir


def _top_chunk_seq(app: FNDApp) -> int | None:
    """The structural chunk whose region spans the preview viewport top."""
    c = app._active_preview
    if c is None:
        return None
    pane = app.query_one("#preview_pane")
    top = pane.scrollable_content_region.y
    for seq, w in c.chunk_widgets.items():
        r = w.region
        if r.height > 0 and r.y <= top < r.y + r.height:
            return seq
    return None


@pytest.mark.asyncio
async def test_reading_view_preserves_match_position(tmp_path: Path, tmp_index_dir: Path) -> None:
    """Toggling Reading View (full-width reflow) keeps the match on screen when
    parked on it. The structural reflow re-wraps asynchronously, so the exact
    top row can drift a chunk; the guarantee is that the match chunk stays in
    the viewport (the flat path is exact — see the scrolled-position test)."""
    index = _reading_doc(tmp_path, tmp_index_dir)
    app = FNDApp(index_dir=index, initial_query="quartzfin-anchor")
    async with app.run_test(size=(120, 40)) as pilot:
        pane = app.query_one("#preview_pane")
        await wait_until(
            pilot,
            lambda: (
                app._active_preview is not None
                and pane.scroll_y > 0
                and _top_chunk_seq(app) is not None
            ),
            timeout=15.0,
            message="structural preview never scrolled to match",
        )
        assert app._preview_scroll.is_armed
        anchor = app._preview_scroll.anchor
        assert anchor is not None
        match_seq = anchor.focus_chunk_seq

        app.action_toggle_reading_mode()
        await settle(pilot, ticks=12)

        assert app._reading_mode is True
        c = app._active_preview
        assert c is not None
        w = c.chunk_widgets.get(match_seq)
        assert w is not None
        assert w.region.height > 0, "match chunk not laid out after toggle"
        vtop = pane.scrollable_content_region.y
        vbot = vtop + pane.scrollable_content_region.height
        # The match chunk must overlap the viewport (it may start above the top
        # when the match sits a quarter of the way down a tall chunk).
        overlaps_viewport = w.region.y < vbot and w.region.y + w.region.height > vtop
        assert overlaps_viewport, (
            f"match chunk {match_seq} (region={w.region}) left the viewport "
            f"[{vtop}, {vbot}) after the Reading View toggle"
        )


@pytest.mark.asyncio
async def test_reading_view_preserves_scrolled_position(
    tmp_path: Path, tmp_index_dir: Path
) -> None:
    """When the user has scrolled away from the match, Reading View preserves
    THEIR position — not the match."""
    index = _reading_doc(tmp_path, tmp_index_dir)
    app = FNDApp(index_dir=index, initial_query="quartzfin-anchor")
    async with app.run_test(size=(120, 40)) as pilot:
        pane = app.query_one("#preview_pane")
        await wait_until(
            pilot,
            lambda: (
                app._active_preview is not None
                and pane.scroll_y > 0
                and _top_chunk_seq(app) is not None
            ),
            timeout=15.0,
            message="structural preview never scrolled to match",
        )
        # User scrolls up to a different spot (releases the match anchor).
        app._preview_scroll.release()
        pane.scroll_to(y=max(0, pane.scroll_y // 2), animate=False, immediate=True)
        await settle(pilot, ticks=4)
        before = _top_chunk_seq(app)
        assert before is not None

        app.action_toggle_reading_mode()
        await settle(pilot, ticks=12)

        after = _top_chunk_seq(app)
        assert after == before, (
            f"scrolled position not preserved across Reading View toggle: {before} -> {after}"
        )
