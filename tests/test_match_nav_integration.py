"""End-to-end: n/b navigate between the two CRC matches in a flashcards
table taller than the viewport, in the real FNDApp — including reaching the
second match cell that sits below the fold in a multi-section document.

The reported bug: after the multi-chunk preview settled, the navigator held a
stale (empty) snapshot of stops, so n did nothing and the off-screen second
match was unreachable. These tests assert the second cell actually becomes
visible, not merely that the scroll offset moved.
"""

from __future__ import annotations

import contextlib
import textwrap
from pathlib import Path
from typing import Any

import pytest
from textual.containers import VerticalScroll
from textual.coordinate import Coordinate
from textual.pilot import Pilot
from textual.widgets import DataTable, Tree

from fnd.config import Config, load
from fnd.index import build_index
from fnd.tui import FNDApp
from tests._pilot_wait import safe_press, settle, wait_until


def _write(p: Path, body: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")


@pytest.fixture
def cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        textwrap.dedent("""
            [[collections.notes.sources]]
            path = "/tmp/notes"
        """),
        encoding="utf-8",
    )
    monkeypatch.setattr("fnd.config.default_config_path", lambda: cfg_path)
    return load(cfg_path)


def _table_rows() -> str:
    rows = "".join(
        f"| {i} | question {i} filler filler | answer {i} filler filler |\n" for i in range(1, 32)
    )
    rows += "| 32 | Ethernet Type II Frame | link-layer frame with a CRC checksum |\n"
    rows += "".join(
        f"| {i} | question {i} filler filler | answer {i} filler filler |\n" for i in range(33, 47)
    )
    rows += "| 47 | What is the Ethernet CRC field | Cyclic Redundancy Check field |\n"
    return rows


@pytest.fixture
def flashcards_index(tmp_path: Path, tmp_index_dir: Path) -> Path:
    """Two ADJACENT matching results: the flashcards table (two CRC cells, taller
    than the viewport) and a short paragraph (one CRC). Focusing the table mounts
    both, so the paragraph's match is a mounted stop that the table's scoped
    ``n``/``b`` must exclude — the scoping guarantee, testable without relying on
    a distant chunk background-mounting."""
    a = tmp_path / "notes"
    body = (
        "# Networking Notes\n\n"
        "## Study Flashcards\n\n| # | Q | A |\n| --- | --- | --- |\n"
        + _table_rows()
        + "\n## Summary\n\nA short note on the CRC field.\n"
    )
    _write(a / "Cards.md", body)
    build_index(roots=[a], index_dir=tmp_index_dir, collection="notes")
    return tmp_index_dir


def _current_stop_count(app: FNDApp) -> int:
    """Scoped stops for the focused result, or -1 while the scope is unresolved.

    ``_chunk_stops`` falls back to the UNSCOPED set when the chunk extent is
    unknown, so a bare count stops the walk on a transient match between two
    different sets — and the extent assertion below it then fails."""
    pane = app.query_one("#preview_pane", VerticalScroll)
    nav = app._match_nav
    if nav._current_chunk_extent(pane) is None:
        return -1
    return len(nav._chunk_stops(pane))


async def _walk_to_stop_count(pilot: Pilot[None], app: FNDApp, want: int, key: str) -> bool:
    """Press ``key`` in the results tree until the current result has ``want``
    match stops (i.e. the multi-view table becomes the focused, mounted result).

    Waits on the count itself after each press, not on a run of pauses: under
    load those degrade to no-op yields and the walk presses past the result."""
    for _ in range(10):
        if _current_stop_count(app) == want:
            return True
        await safe_press(pilot, key)
        try:
            # Caught below as control flow, so no snapshot: it is three
            # region walks fired mid-navigation.
            await wait_until(
                pilot, lambda: _current_stop_count(app) == want, timeout=10.0, quiet=True
            )
        except AssertionError:
            continue
        return True
    return _current_stop_count(app) == want


@pytest.fixture
def table_result_index(tmp_path: Path, tmp_index_dir: Path) -> Path:
    """Same multi-section file, but only the flashcards table matches — so the
    table IS the loaded result (the reported scenario). Card 32 is revealed on
    load; card 47's cell sits below the fold within the same result."""
    a = tmp_path / "notes"
    body = (
        "# Networking Notes\n\n"
        "## Overview\n\nThe frame check uses a checksum value for integrity.\n\n"
        "## Detail\n\nSome unrelated prose about switches and routers here.\n\n"
        "## More Detail\n\nMore unrelated prose about addressing and subnets.\n\n"
        "## Study Flashcards\n\n| # | Q | A |\n| --- | --- | --- |\n" + _table_rows()
    )
    _write(a / "Cards.md", body)
    build_index(roots=[a], index_dir=tmp_index_dir, collection="notes")
    return tmp_index_dir


@pytest.fixture
def fence_index(tmp_path: Path, tmp_index_dir: Path) -> Path:
    """One fence taller than the viewport, carrying many matches. The shape the
    per-row stop set exists for: one stop per BLOCK gives this chunk a single
    stop, and every match after the first is unreachable."""
    a = tmp_path / "notes"
    lines = [f"    filler_value_{i} = {i}" for i in range(90)]
    for i in (5, 25, 45, 65, 85):
        lines[i] = f"    total = quartzfin(argument_{i})"
    _write(a / "Code.md", "# Code\n\n```python\n" + "\n".join(lines) + "\n```\n")
    build_index(roots=[a], index_dir=tmp_index_dir, collection="notes")
    return tmp_index_dir


def _painted_rows(app: FNDApp, spec: Any) -> list[int]:
    """Content-space rows of the focused chunk that PAINT a match, from the
    compositor's own strips rather than from any region the navigator reads."""
    from textual._compositor import Compositor
    from textual.geometry import Size

    from fnd.render import text_has_any_match

    pane = app.query_one("#preview_pane", VerticalScroll)
    anchor = app._preview_scroll.anchor
    if anchor is None:
        return []
    chunk = (app._preview.chunk_widgets or {}).get(anchor.focus_chunk_seq)
    if chunk is None or chunk.region.height == 0:
        return []
    size = Size(chunk.size.width, max(chunk.size.height, chunk.virtual_size.height))
    comp = Compositor()
    comp.reflow(chunk, size)
    base = pane.scrollable_content_region.offset.y - pane.scroll_offset.y
    top = chunk.region.y - base
    return [
        top + i
        for i, strip in enumerate(comp.render_strips(size))
        if text_has_any_match(strip.text, spec)
    ]


@pytest.mark.asyncio
async def test_n_walks_every_match_of_a_tall_fence_into_view(
    cfg: Config, fence_index: Path
) -> None:
    """The reported bug, end to end: a fence taller than the viewport holds five
    matches and n must bring each of them on screen. Asserted against the rows
    the compositor PAINTS, so a stop set that merely reports itself as complete
    cannot satisfy it."""
    app = FNDApp(index_dir=fence_index, config=cfg, collection="notes", initial_query="quartzfin")
    async with app.run_test(size=(100, 24)) as pilot:
        await pilot.pause()
        app.query_one("#results_pane", Tree).focus()
        nav = app._match_nav
        pane = app.query_one("#preview_pane", VerticalScroll)
        await wait_until(
            pilot,
            lambda: len(nav._chunk_stops(pane)) >= 5,
            timeout=30.0,
            message="the fence's stops never resolved",
        )
        spec = app._effective_match_spec
        painted = _painted_rows(app, spec)
        assert len(painted) == 5, f"the fixture painted {len(painted)} matches"

        def _on_screen() -> set[int]:
            lo = pane.scroll_offset.y
            return {y for y in painted if lo <= y < lo + pane.scrollable_content_region.height}

        seen = _on_screen()
        assert len(seen) < 5, "the fixture must not fit on one screen"
        for _ in range(len(painted) + 2):
            if len(seen) == len(painted):
                break
            app.action_nav_next_match()
            await pilot.pause()
            await pilot.pause()
            seen |= _on_screen()

        assert seen == set(painted), f"n never brought {sorted(set(painted) - seen)} on screen"


def _cell_visible(pane: VerticalScroll, dt: DataTable[Any], coord: Coordinate) -> bool:
    """True if the table cell at ``coord`` is within the pane's visible area."""
    try:
        cell = dt._get_cell_region(coord)  # type: ignore[attr-defined]
    except Exception:
        return False
    if cell.height == 0:
        return False
    screen = cell.translate(dt.region.offset - dt.scroll_offset)
    vis = pane.scrollable_content_region
    return screen.y >= vis.y and screen.y < vis.y + vis.height


@pytest.mark.asyncio
async def test_n_reveals_second_table_match_below_the_fold(
    cfg: Config, table_result_index: Path
) -> None:
    """When the table IS the current result, n reveals its off-screen second
    match cell — the reported bug, at the geometry level (cell truly visible)."""
    app = FNDApp(index_dir=table_result_index, config=cfg, collection="notes", initial_query="CRC")
    async with app.run_test(size=(110, 24)) as pilot:
        await pilot.pause()
        app.query_one("#results_pane", Tree).focus()

        # count is derived by an async chain after the deep-table preview mounts;
        # on the slower Windows CI runner that mount can outlast the single
        # post-mount count-tick, which then samples the still-composing table and
        # settles low with nothing to re-fire it during a passive wait. Re-run
        # rebuild() each poll so the count re-samples the current subtree and
        # reflects the table the moment it finishes composing (rebuild is the same
        # idempotent call the app makes on search/mount — no product change).
        def _enumerated() -> bool:
            if app._match_nav.count >= 2:
                return True
            app._match_nav.rebuild()
            return False

        await wait_until(
            pilot,
            _enumerated,
            timeout=60.0,
            message="match-nav did not enumerate the table matches after settle",
        )
        pane = app.query_one("#preview_pane", VerticalScroll)
        dt = next(t for t in pane.query(DataTable) if getattr(t, "_fnd_match_coords", []))
        card47 = Coordinate(46, 1)

        # Card 47 starts below the fold — unreachable without navigation.
        assert not _cell_visible(pane, dt, card47), "card 47 unexpectedly already visible"

        for _ in range(app._match_nav.count + 1):
            if _cell_visible(pane, dt, card47):
                break
            app.action_nav_next_match()
            await pilot.pause()
            await pilot.pause()
        assert _cell_visible(pane, dt, card47), "n never revealed card 47's match cell"


@pytest.mark.asyncio
async def test_n_never_scrolls_a_neighbours_match_under_the_current_result(
    cfg: Config, flashcards_index: Path
) -> None:
    """n/b hop between the CURRENT result's hidden matches. Leaving one is a
    hand-over that moves the results selection (see the hand-over tests below) —
    never a silent scroll that brings a neighbour's match on screen while the
    selected row still names this section.

    Tested at the mechanism, not via a distant chunk background-mounting: navigate
    to the multi-view table (which focus-mounts it AND its adjacent Summary
    result), then assert the scoped stop set excludes the neighbour's match that
    the unscoped set includes, and that every press either stays inside the scope
    or hands over explicitly.
    """
    app = FNDApp(index_dir=flashcards_index, config=cfg, collection="notes", initial_query="CRC")
    async with app.run_test(size=(110, 24)) as pilot:
        await pilot.pause()
        app.query_one("#results_pane", Tree).focus()
        nav = app._match_nav
        # Walk the results arrows until the focused result is the two-match table.
        assert await _walk_to_stop_count(pilot, app, 2, "down"), (
            "results arrows never landed on the two-match flashcards table"
        )
        pane = app.query_one("#preview_pane", VerticalScroll)

        # Scoping is active (the table's chunk extent resolved) and captures both
        # of the table's matches — no more, no less.
        assert nav._current_chunk_extent(pane) is not None, "chunk extent did not resolve"
        assert len(nav._chunk_stops(pane)) == 2, "scoped stops should be the table's two matches"
        # The adjacent Summary result's match is mounted too, so the UNSCOPED set
        # is larger — proving the scope is actively excluding another result.
        # That neighbour arrives on the background fill, so it is waited for:
        # asserting it straight away tested how fast the runner mounts.
        await wait_until(
            pilot,
            lambda: len(nav._region_stops(pane)) > len(nav._chunk_stops(pane)),
            timeout=30.0,
            message="the neighbouring result's match never mounted, so scope excludes nothing",
        )

        table_seq = _focus_seq(app)
        for _ in range(5):
            # `_go` early-returns when the chunk's stops are not resolvable yet,
            # which leaves `_last_rel` unset — so wait for the scope BEFORE
            # pressing. Gating after the press would make the assertion about
            # the layout's timing rather than about n.
            await wait_until(
                pilot,
                lambda: len(nav._chunk_stops(pane)) == 2,
                timeout=30.0,
                message="the table's two scoped stops never resolved",
            )
            app.action_nav_next_match()
            # CAPTURE it: `_go` records the landing synchronously, and a
            # background mount completing during the drain below fires a result
            # reveal, which clears `_last_rel` by design. Both assertions are
            # about what the press recorded, so both read the captured value.
            landed = nav._last_rel
            await pilot.pause()
            await pilot.pause()
            if _focus_seq(app) != table_seq:
                # The hand-over: explicit, and the results row names where we are.
                await wait_until(
                    pilot,
                    lambda: _cursor_section_seq(app) == _focus_seq(app),
                    timeout=30.0,
                    message="n left the table without moving the results selection",
                )
                return
            assert landed is not None, "n did not record a landing stop"
            stops = nav._chunk_stops(pane)
            assert len(stops) == 2, "n changed the scoped stop set while the result stayed put"
            extent = nav._current_chunk_extent(pane)
            assert extent is not None, "the chunk extent stopped resolving mid-walk"
            lo, hi = extent
            # `landed` is an offset from the chunk's top, so the bound is the
            # chunk's HEIGHT. Comparing it against the absolute extent passes
            # only while the chunk happens to start near the top of the document.
            assert 0 <= landed < hi - lo, "n landed outside the current result's chunk"


def _focus_seq(app: FNDApp) -> int | None:
    anchor = app._preview_scroll.anchor
    return None if anchor is None else anchor.focus_chunk_seq


def _cursor_section_seq(app: FNDApp) -> int | None:
    """The chunk seq of the results row the cursor is on, or None."""
    node = app.query_one("#results_pane", Tree).cursor_node
    data = getattr(node, "data", None)
    if not isinstance(data, dict) or data.get("kind") != "section":
        return None
    return data["hit"].chunk_seq


async def _walk_until_handover(
    pilot: Pilot[None], app: FNDApp, *, presses: int = 6
) -> tuple[int, int]:
    """Press n until the focused section changes; return the (seq, scroll_y) the
    last press departed from."""
    pane = app.query_one("#preview_pane", VerticalScroll)
    start = _focus_seq(app)
    departed = (start or 0, pane.scroll_offset.y)
    for _ in range(presses):
        departed = (_focus_seq(app) or 0, pane.scroll_offset.y)
        before = pane.scroll_offset.y
        app.action_nav_next_match()
        # Gate on the press's own effect, not on a tick count that degrades to a
        # no-op under load. A press that moves nothing ends the walk, so the
        # timeout is control flow rather than a failure.
        with contextlib.suppress(AssertionError):
            await wait_until(
                pilot,
                lambda: _focus_seq(app) != start or pane.scroll_offset.y != before,
                timeout=10.0,
                quiet=True,
            )
        if _focus_seq(app) != start:
            return departed
    return departed


@pytest.mark.asyncio
async def test_n_hands_over_to_the_next_listed_section(cfg: Config, flashcards_index: Path) -> None:
    """Exhausting the table's own views does not wrap forever inside it: the
    next press moves to the file's next listed section, and the results cursor
    moves with it so the row and the border name the same section."""
    app = FNDApp(index_dir=flashcards_index, config=cfg, collection="notes", initial_query="CRC")
    async with app.run_test(size=(110, 24)) as pilot:
        await pilot.pause()
        app.query_one("#results_pane", Tree).focus()
        assert await _walk_to_stop_count(pilot, app, 2, "down"), (
            "results arrows never landed on the two-match flashcards table"
        )
        start = _focus_seq(app)
        await _walk_until_handover(pilot, app)
        assert _focus_seq(app) != start, "n never left the table's own views"
        await wait_until(
            pilot,
            lambda: _cursor_section_seq(app) == _focus_seq(app),
            timeout=30.0,
            message="the results cursor did not follow the hand-over",
        )


@pytest.mark.asyncio
async def test_b_returns_across_a_hand_over_to_the_sections_last_view(
    cfg: Config, flashcards_index: Path
) -> None:
    """b undoes the hand-over: back to the section n left, and to its LAST view —
    the end n departed from, not the landing a results row would give.

    Asserted on the stop the viewport SHOWS, not on the scroll offset it left:
    swapping a live chunk for its capture re-heights the document, so the same
    view is a different absolute y on the way back.
    """
    app = FNDApp(index_dir=flashcards_index, config=cfg, collection="notes", initial_query="CRC")
    async with app.run_test(size=(110, 24)) as pilot:
        await pilot.pause()
        app.query_one("#results_pane", Tree).focus()
        assert await _walk_to_stop_count(pilot, app, 2, "down"), (
            "results arrows never landed on the two-match flashcards table"
        )
        pane = app.query_one("#preview_pane", VerticalScroll)
        nav = app._match_nav
        seq, _top = await _walk_until_handover(pilot, app)
        assert _focus_seq(app) != seq, "n never handed over, so there is nothing to undo"

        def _last_stop_on_screen() -> bool:
            if _focus_seq(app) != seq or _cursor_section_seq(app) != seq:
                return False
            stops = nav._chunk_stops(pane)
            lo = pane.scroll_offset.y
            return bool(stops) and lo <= stops[-1] < lo + pane.scrollable_content_region.height

        app.action_nav_prev_match()
        await wait_until(
            pilot,
            _last_stop_on_screen,
            timeout=30.0,
            message=(
                f"b did not return to the departed section's last view: "
                f"focus={_focus_seq(app)} (want {seq})"
            ),
        )


@pytest.mark.asyncio
async def test_a_hand_over_lands_even_after_an_option_scan(
    cfg: Config, flashcards_index: Path
) -> None:
    """Option+arrow browsing leaves the preview deliberately behind the cursor,
    and only the results tree's own key handler clears that — the preview pane
    has focus for n/b just as often, so the hand-over clears it itself."""
    app = FNDApp(index_dir=flashcards_index, config=cfg, collection="notes", initial_query="CRC")
    async with app.run_test(size=(110, 24)) as pilot:
        await pilot.pause()
        app.query_one("#results_pane", Tree).focus()
        assert await _walk_to_stop_count(pilot, app, 2, "down"), (
            "results arrows never landed on the two-match flashcards table"
        )
        start = _focus_seq(app)
        app._preview._scan_move = True

        await _walk_until_handover(pilot, app)

        assert _focus_seq(app) != start, "the scan flag suppressed the hand-over's load"


@pytest.fixture
def flat_index(tmp_path: Path, tmp_index_dir: Path) -> Path:
    """A .txt file — no chunk is markdown-rendered, so the preview takes the
    FLAT path, which nulls ``preview.active`` and keeps its own buffer."""
    a = tmp_path / "notes"
    filler = "".join(f"filler line {i} of no interest at all.\n" for i in range(60))
    body = (
        "opening line mentions quartzfin early.\n"
        + filler
        + "a second quartzfin sits far below the first.\n"
        + filler
        + "the closing quartzfin is the last one in the file.\n"
    )
    _write(a / "Plain.txt", body)
    build_index(roots=[a], index_dir=tmp_index_dir, collection="notes")
    return tmp_index_dir


@pytest.mark.asyncio
async def test_a_flat_preview_advertises_no_match_keys(cfg: Config, flat_index: Path) -> None:
    """A line buffer contributes no stops — ``enumerate_stop_regions`` reads
    markdown blocks, captures and per-line Statics, and a flat preview has none
    — so n/b are inert there and the footer must not offer them.

    The hand-over is what makes this worth pinning: reading the flat file's
    identity from its installed buffer made ``can_hop_section`` true, and the
    hint appeared over keys that still did nothing.
    """
    app = FNDApp(index_dir=flat_index, config=cfg, collection="notes", initial_query="quartzfin")
    async with app.run_test(size=(110, 24)) as pilot:
        await pilot.pause()
        app.query_one("#results_pane", Tree).focus()
        await wait_until(
            pilot,
            lambda: app._flat.active_buffer is not None and _cursor_section_seq(app) is not None,
            timeout=30.0,
            message="the flat buffer never activated on a .txt file",
        )
        assert app._preview.active is None, "this fixture must take the flat path"
        nav = app._match_nav
        pane = app.query_one("#preview_pane", VerticalScroll)

        assert nav._chunk_stops(pane) == [], "a flat preview resolved stops it cannot have"
        assert not nav.current_chunk_has_stops(), "the footer offered n/b where they do nothing"


@pytest.mark.asyncio
async def test_a_hand_over_onto_the_row_the_cursor_already_holds_still_lands(
    cfg: Config, flashcards_index: Path
) -> None:
    """An Option-scan moves the results cursor without loading, so the cursor can
    already be on the row a hand-over targets. Textual's ``move_cursor_to_line``
    early-returns there and no highlight fires — the press has to dispatch the
    load itself, or it reports success having done nothing.

    The scan state is built through the app's own mechanism (``_scan_move`` plus
    a cursor move), because pressing Option+arrow depends on key routing this
    test is not about.
    """
    app = FNDApp(index_dir=flashcards_index, config=cfg, collection="notes", initial_query="CRC")
    async with app.run_test(size=(110, 24)) as pilot:
        await pilot.pause()
        tree = app.query_one("#results_pane", Tree)
        tree.focus()
        assert await _walk_to_stop_count(pilot, app, 2, "down"), (
            "results arrows never landed on the two-match flashcards table"
        )
        start = _focus_seq(app)
        rows = [
            (ln, tl.node.data["hit"].chunk_seq)
            for ln, tl in enumerate(tree._tree_lines)
            if isinstance(tl.node.data, dict) and tl.node.data.get("kind") == "section"
        ]
        target = next((ln, seq) for ln, seq in rows if seq != start)

        # Scan: the cursor moves, the preview deliberately does not follow.
        app._preview._scan_move = True
        tree.cursor_line = target[0]
        await settle(pilot, 3)
        assert _cursor_section_seq(app) == target[1], "the scan did not move the cursor"
        assert _focus_seq(app) == start, "the scan loaded the row it moved onto"

        # b at the landing has nothing above it inside this chunk, so it hands
        # over — onto the row the cursor already holds.
        app.action_nav_prev_match()

        await wait_until(
            pilot,
            lambda: _focus_seq(app) == target[1],
            timeout=30.0,
            message=(
                f"the hand-over onto the cursor's own row never landed: "
                f"focus={_focus_seq(app)} cursor={_cursor_section_seq(app)}"
            ),
        )


@pytest.mark.asyncio
async def test_a_hand_over_into_a_collapsed_file_still_lands(
    cfg: Config, flashcards_index: Path
) -> None:
    """A reader can collapse the file they are previewing; its section rows then
    have no line in the tree, so the hand-over has to open it before it can put
    the cursor on one.

    The focus is re-read AFTER the collapse: collapsing moves the cursor onto the
    file row, which loads it, so the section captured before the collapse is not
    the one the press starts from.
    """
    app = FNDApp(index_dir=flashcards_index, config=cfg, collection="notes", initial_query="CRC")
    async with app.run_test(size=(110, 24)) as pilot:
        await pilot.pause()
        tree = app.query_one("#results_pane", Tree)
        tree.focus()
        assert await _walk_to_stop_count(pilot, app, 2, "down"), (
            "results arrows never landed on the two-match flashcards table"
        )
        file_node = next(
            node for node in tree.root.children if node.data and node.data.get("kind") == "file"
        )
        file_node.collapse()
        await settle(pilot, 4)
        assert not file_node.is_expanded
        start = _focus_seq(app)

        app.action_nav_prev_match()

        await wait_until(
            pilot,
            lambda: file_node.is_expanded and _focus_seq(app) not in (None, start),
            timeout=30.0,
            message=(
                f"the hand-over never landed out of a collapsed file: "
                f"focus={_focus_seq(app)} (start {start}) expanded={file_node.is_expanded}"
            ),
        )
