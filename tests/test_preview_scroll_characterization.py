"""Characterization net for preview scroll-to-match.

Pins the observable scroll behaviour of the centralised scroll controller.
Each test asserts the visible outcome (match on-screen / scroll position),
mirroring ``tests/test_preview_scrolls_to_match.py``. The cold file-node
navigation case captured a known off-screen bug; the controller fixes it, so
it is now a hard-asserting regression test (no longer xfailed).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path

import pytest
from textual.geometry import Region
from textual.pilot import Pilot, WaitForScreenTimeout
from textual.widget import Widget
from textual.widgets import DataTable, Tree

from fnd.config import Config, Defaults, RankingProfileConfig
from fnd.index import build_index
from fnd.query import FileGroup
from fnd.tui import FNDApp
from fnd.tui.line_buffer import LineBufferPreview
from fnd.tui.preview.frozen import FrozenChunkView
from fnd.tui.preview.presenter import PreviewPresenter
from tests._pilot_wait import run_search, safe_pause, settle, wait_stable, wait_until


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
                bool(app._search.groups)
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
            lambda: app._flat.active_buffer is not None and app._flat.active_buffer._fv is not None,
            timeout=15.0,
            message="initial flat buffer never activated",
        )
        # Snapshot the FileView the first query installed; the second
        # query either swaps in a new FileView or clears it. Without
        # this token the predicate can match the first query's already-
        # scrolled buffer before the second has rewired it.
        pre_fv = app._flat.active_buffer._fv  # type: ignore[union-attr]
        await run_search(pilot, app, "blue penguin sandwich")
        await wait_until(
            pilot,
            lambda: (
                app._flat.active_buffer is not None
                and app._flat.active_buffer._fv is not pre_fv
                and (app._flat.active_buffer.scroll_y > 0 or not app._flat.active_buffer._fv)
            ),
            timeout=15.0,
            message="flat buffer never settled after second query",
        )
        active = app._flat.active_buffer
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
    from fnd.tui.widgets.markdown import FNDMarkdownTableDT

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
            lambda: len(app._search.groups) >= 2,
            timeout=15.0,
            message="results never accumulated 2 groups",
        )
        for i, _g in enumerate(app._search.groups):
            expected_parent = app._search.groups[i].parent_id
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
                    app._preview.active is not None
                    and app._preview.active.parent_doc_id == parent
                    and pane.scroll_y > 0
                ),
                timeout=20.0,
                message=(
                    f"result {i} parent={expected_parent} "
                    f"active={app._preview.active.parent_doc_id if app._preview.active else None} "
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


def _build_coldnav_app(tmp_path: Path, tmp_index_dir: Path) -> FNDApp:
    """Index four structural files and build an app with prefetch ON and the
    preview cache lifted, so a non-first file can stay pre-mounted and the
    cold / prefetched-container nav path is exercised.

    Prefetch must be ON: the autouse conftest fixture pins
    ``preview_prefetch_count=0``; an explicit ``Defaults`` value overrides it.
    The shipped cache caps at 1 (see ``_PREVIEW_CACHE_MAX_FILES``); lifting
    ``max_files`` lets the rank-1 file stay pre-mounted (the decode is
    prefetched regardless of cache size).
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
    app._preview.preview_cache.max_files = 8
    return app


def _coldnav_match_region(
    app: FNDApp, parent_id: str, focus_seq: int
) -> Callable[[], Region | None]:
    """Return a probe for the region of the widget holding the unique query
    text in the target file — the prose match the cold-nav scroll must land on.
    The probe returns None until that widget is laid out in the active preview.
    """

    def _region() -> Region | None:
        ap = app._preview.active
        if ap is None or ap.parent_doc_id != parent_id:
            return None
        chunk = ap.match_targets.get(focus_seq) or ap.chunk_widgets.get(focus_seq)
        if chunk is None:
            return None
        # A frozen chunk has no child widgets to walk — that is what freezing
        # is — so its match resolves from the row recorded at capture time, the
        # way ``enumerate_stop_regions`` resolves one. Walking children only
        # made this probe answer None for good once the sweep reached the focus
        # chunk, and every wait gated on it then ran out its whole budget.
        if isinstance(chunk, FrozenChunkView):
            row = chunk.frozen.first_match_row
            # Same bounds check as ``enumerate_stop_regions``: a clipped view
            # resolves nothing there, and a probe that answers anyway is
            # claiming a parity it does not have.
            if row is None or not (0 <= row < chunk.region.height):
                return None
            return Region(chunk.region.x, chunk.region.y + row, chunk.region.width, 1)
        for w in chunk.query("*"):
            if w is chunk:
                continue
            plain = getattr(getattr(w, "_content", None), "plain", None)
            if plain and "quartzfin" in plain and w.region.height > 0:
                return w.region
        return None

    return _region


async def _coldnav_run_query_and_prefetch(app: FNDApp, pilot: Pilot[None]) -> FileGroup:
    """Run the query, wait for >=3 result groups, then wait for the rank-1
    (non-first) file to be prefetched + pre-mounted so navigation hits the
    cold/prefetched-container code path. Returns the rank-1 group."""
    await run_search(pilot, app, "quartzfin")
    sig = app._search.query_signature()
    await wait_until(
        pilot,
        lambda: len(app._search.groups) >= 3,
        timeout=15.0,
        message="results never accumulated 3 groups",
    )
    assert len(app._search.groups) >= 3
    # Match is early-middle, not chunk 0 and not the last chunk.
    assert app._search.groups[0].hits[0].chunk_seq > 0, "match should not be in the first chunk"

    target_group = app._search.groups[1]
    nudged = False

    def _pre_mounted() -> bool:
        # Event-driven wall-clock wait (not an iteration cap, which slow
        # prefetch decode on a serial CI runner outruns). The initial deferred
        # prefetch bails if the rank-0 user-mount was still in flight when it
        # walked targets; nudge once that clears so the target gets re-queued.
        nonlocal nudged
        cont = app._preview.preview_cache.get(target_group.parent_id, sig)
        if cont is not None and cont.mounted_indices:
            return True
        if not nudged and not app._preview.user_mount_in_flight():
            app._prefetch.prefetch_top_results()
            nudged = True
        return False

    await wait_until(
        pilot,
        _pre_mounted,
        timeout=20.0,
        message=f"prefetch never pre-mounted {target_group.parent_id}",
    )
    prefetched = app._preview.preview_cache.get(target_group.parent_id, sig)
    assert prefetched is not None, f"prefetch never built {target_group.parent_id}"
    assert prefetched.mounted_indices, f"prefetch never pre-mounted {target_group.parent_id}"
    return target_group


@pytest.mark.asyncio
async def test_cold_nav_to_prefetched_non_first_file_lands_on_screen(
    tmp_path: Path, tmp_index_dir: Path
) -> None:
    """Cold file-node navigation to a prefetched non-first structural file
    lands the (correctly-resolved) prose match on-screen, ~25% down.

    Regression guard for the cold-nav under-shoot the scroll controller fixes:
    navigating to a prefetched container must drop the match ~25% down the
    viewport, not leave it below the fold. ``scroll_y > 0`` is NOT a sufficient
    landed-signal here: the pane carries residual scroll from the previously
    previewed file AND the prefetched focus chunk is already mounted, so both
    ``scroll_y > 0`` and the match widget exist long before THIS navigation has
    scrolled — asserting then reads the pre-landing position (the flake). The
    controller exposes the real landed signal, ``is_settling`` (armed and the
    nav's scroll has not committed); gating on ``not is_settling`` runs the
    assert only once the match scroll has landed. The deterministic counterpart
    that forces the lagging-landing window is
    ``test_cold_nav_delayed_landing_waits_for_real_settle``.
    """
    app = _build_coldnav_app(tmp_path, tmp_index_dir)
    async with app.run_test(size=(120, 40)) as pilot:
        pane = app.query_one("#preview_pane")
        rtree = app.query_one("#results_pane", Tree)
        target_group = await _coldnav_run_query_and_prefetch(app, pilot)
        focus_seq = target_group.hits[0].chunk_seq
        match_region = _coldnav_match_region(app, target_group.parent_id, focus_seq)

        # Navigate the user to the (collapsed, non-first) target file node —
        # closest to the real user action — and drive the cold load.
        rtree.focus()
        await safe_pause(pilot)
        rtree.move_cursor(rtree.root.children[1])

        # Gate on the real landed signal — NOT ``scroll_y > 0`` (true from the
        # prior file's residual scroll before this nav moves at all). Once the
        # controller is no longer settling, the match scroll has committed.
        await wait_until(
            pilot,
            lambda: (
                app._preview.active is not None
                and app._preview.active.parent_doc_id == target_group.parent_id
                and not app._preview_scroll.is_settling
                and match_region() is not None
            ),
            timeout=20.0,
            message="cold-nav target never activated / content match never laid out",
        )
        await settle(pilot)

        region = match_region()
        assert region is not None, "content match widget never laid out"
        # The prose match must be inside the pane viewport — scroll_y > 0 alone
        # is not enough; the under-shoot scrolls but stops short of the match.
        top, bottom = pane.region.y, pane.region.y + pane.region.height
        assert top <= region.y < bottom, (
            f"content match at screen y={region.y} is outside the preview "
            f"viewport [{top}, {bottom}) — cold-render scroll under-shot, leaving "
            f"the match below the fold (pane.scroll_y={pane.scroll_y})"
        )


@pytest.mark.asyncio
async def test_cold_nav_delayed_landing_waits_for_real_settle(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, tmp_index_dir: Path
) -> None:
    """Deterministic counterpart to the cold-nav landing test: force the match
    scroll to land LATE (what full-suite load does intermittently) and assert
    that gating on the controller's ``is_settling`` signal still lands the match
    in the viewport.

    The original flake was the test asserting on a fixed-tick settle that
    expired before this late scroll committed (``scroll_y > 0`` + a mounted
    match widget are both true well before THIS nav scrolls — see the sibling
    test's docstring). Here the finalise settle is delayed so the correcting
    scroll is guaranteed late; gating on ``not is_settling`` waits it out. We
    assert only the post-settle invariant (match in viewport): the pre-landing
    intermediate position is a transient, and sampling it in-suite would itself
    be racy — that the weak proxy trips pre-landing is shown by the dev harness
    in ``dev/tools/coldnav_timeline.py``.
    """
    app = _build_coldnav_app(tmp_path, tmp_index_dir)

    # Delay finalise's pre-scroll settle so the correcting match scroll commits
    # well after the focus chunk has mounted — forcing the lagging-landing path
    # deterministically instead of relying on load timing. ``await_match_settled``
    # is the settle the cold finalise awaits before its single match scroll.
    # Scoped to the TARGET nav only (enabled after prefetch): a global delay also
    # slows the rank-0 auto-load and starves the prefetch-wait under load.
    delay_target_landing = False
    orig_settle = PreviewPresenter.await_match_settled

    async def _slow_settle(
        self: PreviewPresenter, header: object, above_widgets: object, max_rounds: int = 12
    ) -> None:
        if delay_target_landing:
            await asyncio.sleep(0.4)
        await orig_settle(self, header, above_widgets, max_rounds=max_rounds)  # type: ignore[arg-type]

    monkeypatch.setattr(PreviewPresenter, "await_match_settled", _slow_settle)

    async with app.run_test(size=(120, 40)) as pilot:
        pane = app.query_one("#preview_pane")
        rtree = app.query_one("#results_pane", Tree)
        target_group = await _coldnav_run_query_and_prefetch(app, pilot)
        focus_seq = target_group.hits[0].chunk_seq
        match_region = _coldnav_match_region(app, target_group.parent_id, focus_seq)

        rtree.focus()
        await safe_pause(pilot)

        # Latch the settling-entry deterministically: arm() is what flips
        # is_settling True (armed & not settled), so record when the controller
        # arms for the TARGET rather than polling for the transient is_settling.
        # Under heavy CI load a single safe_pause can block longer than the 0.4s
        # injected settling window, so sampling is_settling at poll boundaries
        # intermittently misses it (the documented macOS flake); the latch can't.
        armed_for_target = False
        ctrl = app._preview_scroll
        _orig_arm = ctrl.arm

        def _latching_arm(anchor: object) -> None:
            nonlocal armed_for_target
            if getattr(anchor, "parent_id", None) == target_group.parent_id:
                armed_for_target = True
            _orig_arm(anchor)  # type: ignore[arg-type]

        monkeypatch.setattr(ctrl, "arm", _latching_arm)

        # Arm the delay now — only the target navigation's landing lags.
        delay_target_landing = True
        rtree.move_cursor(rtree.root.children[1])

        # Phase 1: prove the delayed-finalise path actually armed — the nav must
        # enter the settling state on the target before we wait for it to clear,
        # so a swap-in of the prefetched container can't satisfy phase 2 without
        # the controller ever settling. Gate on the latch (not a live is_settling
        # read) so a load spike that closes the window between polls can't fail us.
        await wait_until(
            pilot,
            lambda: armed_for_target,
            timeout=20.0,
            message="cold-nav target never entered settling",
        )
        # Phase 2: gate on the real landed signal — it waits out the delayed
        # correcting scroll that a fixed-tick settle would miss.
        await wait_until(
            pilot,
            lambda: (
                app._preview.active is not None
                and app._preview.active.parent_doc_id == target_group.parent_id
                and not app._preview_scroll.is_settling
                and match_region() is not None
            ),
            timeout=20.0,
            message="match scroll never landed after the controller settled",
        )
        # is_settling clears when the scroll is ISSUED, not when it lands: the
        # scroll may still be animating, and the reveal + the lazy-mount gate it
        # unblocks both move the viewport afterwards. Gate on the offset holding
        # still — a fixed settle here is what let the assertion read mid-glide.
        await wait_stable(
            pilot,
            lambda: pane.scroll_offset.y,
            timeout=20.0,
            message="scroll never stopped moving after the controller settled",
        )
        region = match_region()
        assert region is not None, "content match widget never laid out"
        top, bottom = pane.region.y, pane.region.y + pane.region.height
        assert top <= region.y < bottom, (
            f"with a delayed finalise, the match at y={region.y} is outside "
            f"viewport [{top}, {bottom}) after the controller settled "
            f"(pane.scroll_y={pane.scroll_y}) — gating on is_settling did not "
            f"wait for the real landing"
        )


@pytest.mark.asyncio
async def test_the_landing_probe_survives_the_focus_chunk_freezing(
    tmp_path: Path, tmp_index_dir: Path
) -> None:
    """Freezing must not blind the two cold-nav tests above.

    Their waits end in ``match_region() is not None``. A frozen chunk has no
    child widgets, so a probe that walks children answers None for good the
    moment the sweep reaches the focus chunk — and the wait then burns its whole
    budget on a preview that is correct.
    """
    app = _build_coldnav_app(tmp_path, tmp_index_dir)
    async with app.run_test(size=(120, 40)) as pilot:
        rtree = app.query_one("#results_pane", Tree)
        target_group = await _coldnav_run_query_and_prefetch(app, pilot)
        focus_seq = target_group.hits[0].chunk_seq
        match_region = _coldnav_match_region(app, target_group.parent_id, focus_seq)
        rtree.focus()
        await safe_pause(pilot)
        rtree.move_cursor(rtree.root.children[1])
        await wait_until(
            pilot,
            lambda: (
                app._preview.active is not None
                and app._preview.active.parent_doc_id == target_group.parent_id
                and match_region() is not None
            ),
            timeout=20.0,
            message="cold-nav target never activated",
        )
        container = app._preview.active
        assert container is not None
        chunks = app._preview.chunk_cache[target_group.parent_id]
        mounted = sorted(container.mounted_indices)
        await app._preview._freeze_chunks_outside_window(
            container, chunks, mounted[-1] + 1, mounted[-1] + 1
        )
        frozen = sum(1 for w in container.chunk_widgets.values() if isinstance(w, FrozenChunkView))
        assert frozen, "the sweep froze nothing, so this proves nothing"
        # The stand-in has to lay out before it can resolve a row, and one pause
        # is a wait only while the machine is idle — the defect this whole file
        # is about.
        await wait_until(
            pilot,
            lambda: match_region() is not None,
            timeout=20.0,
            message="the probe went blind once the focus chunk froze",
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
    c = app._preview.active
    if c is None:
        return None
    pane = app.query_one("#preview_pane")
    top = pane.scrollable_content_region.y
    for seq, w in c.chunk_widgets.items():
        r = w.region
        if r.height > 0 and r.y <= top < r.y + r.height:
            return seq
    return None


def _match_in_viewport(app: FNDApp, pane: Widget, match_seq: int) -> bool:
    """Is the match chunk laid out AND overlapping the preview viewport? It may
    start above the top when the match sits part-way down a tall chunk, so this
    is an overlap test, not a containment one."""
    c = app._preview.active
    w = c.chunk_widgets.get(match_seq) if c is not None else None
    if w is None or w.region.height <= 0:
        return False
    vtop = pane.scrollable_content_region.y
    vbot = vtop + pane.scrollable_content_region.height
    return w.region.y < vbot and w.region.y + w.region.height > vtop


async def _match_parked(pilot: Pilot[None], app: FNDApp, pane: Widget, match_seq: int) -> None:
    """Wait until the initial navigation has actually parked on the match.

    ``scroll_y > 0`` and a top chunk existing are both true well before the
    landing finishes, and ``is_settling`` clears when the scroll is ISSUED, so
    neither says the match is on screen yet. Toggling Reading View before it is
    makes ``locate()`` capture a mid-landing position, and the restore then
    faithfully reproduces a position that was never right — which is the whole
    failure, not a reflow bug."""
    await wait_until(
        pilot,
        lambda: _match_in_viewport(app, pane, match_seq),
        timeout=20.0,
        message="initial navigation never parked the match in the viewport",
    )
    await wait_stable(
        pilot,
        lambda: (pane.scroll_offset.y, pane.virtual_size.height),
        timeout=20.0,
        message="preview never stopped moving before the toggle",
    )


async def _reading_reflow_landed(
    pilot: Pilot[None], app: FNDApp, pane: Widget, *, since: int
) -> None:
    """Wait out a Reading View toggle: the widen re-wraps asynchronously and the
    controller re-applies its restore across an unbounded number of refreshes.

    ``since`` is ``restores_completed`` read BEFORE the toggle. Waiting on
    ``not is_restoring`` instead would be vacuous — the toggle only schedules
    the restore via ``call_after_refresh``, so the flag is still False when the
    wait first looks and it returns having proved nothing."""
    await wait_until(
        pilot,
        lambda: app._preview_scroll.restores_completed > since,
        timeout=20.0,
        message="reading-view reflow restore never finished",
    )
    await wait_stable(
        pilot,
        lambda: (pane.scroll_offset.y, pane.virtual_size.height),
        timeout=20.0,
        message="preview never stopped re-wrapping after the toggle",
    )


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
                app._preview.active is not None
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
        await _match_parked(pilot, app, pane, match_seq)

        since = app._preview_scroll.restores_completed
        app.action_toggle_reading_mode()
        await _reading_reflow_landed(pilot, app, pane, since=since)

        assert app._reading_mode is True
        c = app._preview.active
        assert c is not None
        w = c.chunk_widgets.get(match_seq)
        assert w is not None
        assert w.region.height > 0, "match chunk not laid out after toggle"
        vtop = pane.scrollable_content_region.y
        vbot = vtop + pane.scrollable_content_region.height
        assert _match_in_viewport(app, pane, match_seq), (
            f"match chunk {match_seq} (region={w.region}) left the viewport "
            f"[{vtop}, {vbot}) after the Reading View toggle"
        )


@pytest.mark.asyncio
async def test_reading_view_preserves_match_position_under_a_degraded_pause(
    tmp_path: Path, tmp_index_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The load-spike shape, made deterministic.

    A saturated CI runner makes ``pilot.pause()`` hit Textual's internal
    ``_wait_for_screen`` timeout; ``safe_pause`` swallows it into a few
    ``asyncio.sleep(0)`` yields, so a fixed-tick settle flushes almost no
    refreshes. Forcing that timeout on every pause reproduced the CI failure of
    the sibling test above on all three OSes; gating on the restore instead of
    on a tick count survives it."""

    async def _always_times_out(self: Pilot[None], delay: float | None = None) -> None:
        raise WaitForScreenTimeout()

    monkeypatch.setattr(Pilot, "pause", _always_times_out)
    index = _reading_doc(tmp_path, tmp_index_dir)
    app = FNDApp(index_dir=index, initial_query="quartzfin-anchor")
    async with app.run_test(size=(120, 40)) as pilot:
        pane = app.query_one("#preview_pane")
        await wait_until(
            pilot,
            lambda: (
                app._preview.active is not None
                and pane.scroll_y > 0
                and _top_chunk_seq(app) is not None
            ),
            timeout=30.0,
            message="structural preview never scrolled to match",
        )
        anchor = app._preview_scroll.anchor
        assert anchor is not None
        match_seq = anchor.focus_chunk_seq
        await _match_parked(pilot, app, pane, match_seq)

        since = app._preview_scroll.restores_completed
        app.action_toggle_reading_mode()
        await _reading_reflow_landed(pilot, app, pane, since=since)

        c = app._preview.active
        assert c is not None
        w = c.chunk_widgets.get(match_seq)
        assert w is not None
        vtop = pane.scrollable_content_region.y
        vbot = vtop + pane.scrollable_content_region.height
        assert _match_in_viewport(app, pane, match_seq), (
            f"match chunk {match_seq} (region={w.region}) left the viewport "
            f"[{vtop}, {vbot}) under a degraded pause"
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
                app._preview.active is not None
                and pane.scroll_y > 0
                and _top_chunk_seq(app) is not None
            ),
            timeout=15.0,
            message="structural preview never scrolled to match",
        )
        # User scrolls up to a different spot (releases the match anchor).
        app._preview_scroll.release()
        pane.scroll_to(y=max(0, pane.scroll_y // 2), animate=False, immediate=True)
        # ``before`` is a moving target until the scroll lands, and a tick count
        # can flush nothing under load — wait for the geometry itself.
        await wait_stable(
            pilot,
            lambda: (pane.scroll_offset.y, pane.virtual_size.height),
            timeout=20.0,
            message="preview never settled after the user scroll",
        )
        before = _top_chunk_seq(app)
        assert before is not None

        since = app._preview_scroll.restores_completed
        app.action_toggle_reading_mode()
        await _reading_reflow_landed(pilot, app, pane, since=since)

        after = _top_chunk_seq(app)
        assert after == before, (
            f"scrolled position not preserved across Reading View toggle: {before} -> {after}"
        )
