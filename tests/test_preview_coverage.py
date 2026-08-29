"""Coverage captures the matches ahead, and a jump then mounts instead of builds.

A file past ``FULLMOUNT_CHUNK_BUDGET`` is never background-filled, so before this
every jump outside the ±7 window built a fresh container from markdown source —
including to a chunk visited moments earlier, because the rebuild discarded the
one that held it. Measured on a real corpus at ~1743ms against ~243ms for a jump
inside the window.

What is asserted here is the mechanism that closes that gap: that hit chunks get
captured, and that a jump to one MOUNTS the capture rather than building a
markdown widget. Latency itself is not asserted — the suite pins preview debounce
and prefetch to zero, so it cannot reproduce the timings that motivate any of it.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import time
from pathlib import Path
from typing import Any, cast

import pytest
from textual.app import App, ComposeResult
from textual.containers import VerticalScroll
from textual.widget import Widget

from fnd.matching import MatchSpec
from fnd.query import FileChunk
from fnd.tui import FNDApp
from fnd.tui.preview import tuning
from fnd.tui.preview import warm_host as warm_host_mod
from fnd.tui.preview.coverage import coverage_targets, neighbour_order
from fnd.tui.preview.frozen import FrozenChunk, FrozenChunkView, freeze
from fnd.tui.preview.warm_host import WarmHost
from fnd.tui.preview_dispatcher import uses_markdown_renderer
from fnd.tui.widgets.markdown import FNDMarkdown
from tests._pilot_wait import settle, wait_until
from tests._preview_corpus import wide_doc


def test_targets_are_nearest_first_and_bounded() -> None:
    targets = coverage_targets(
        total=1000,
        focus_idx=500,
        hit_indices=[10, 500, 990],
        already=set(),
        margin=1,
        budget=4,
    )
    # The distant hit contributes 11 rather than 10: its margin covers 9-11, and
    # 11 is the side of it nearest the focus. Ordering is by distance from where
    # the user IS, not by hit position.
    assert targets == [500, 499, 501, 11], f"expected nearest-first, got {targets}"


def test_only_hits_and_their_margin_are_captured() -> None:
    """Not the whole file, however small. The one serial warm host is the scarce
    resource: covering a file whole spends it on chunks no jump lands on, while
    the neighbours the cursor needs get nothing."""
    targets = coverage_targets(
        total=20, focus_idx=0, hit_indices=[5], already=set(), margin=1, budget=500
    )
    assert sorted(targets) == [4, 5, 6]


def test_neighbours_alternate_outward_from_the_cursor() -> None:
    """The user is as likely to press up as down, so covering three files below
    before the one immediately above would leave half the navigations unhelped."""
    ids = [f"f{i}" for i in range(7)]
    assert neighbour_order(ids, here=3, span=2) == ["f2", "f4", "f1", "f5"]
    # Clamped at the ends rather than wrapping.
    assert neighbour_order(ids, here=0, span=2) == ["f1", "f2"]


def test_already_held_chunks_are_not_recaptured() -> None:
    targets = coverage_targets(
        total=20, focus_idx=0, hit_indices=[5], already=set(range(10)), margin=1, budget=500
    )
    assert all(i >= 10 for i in targets), targets


def _laid_out_chunk(app: FNDApp) -> Widget | None:
    """A mounted chunk that has actually been through layout.

    ``_preview.active`` only says the container exists; its children report
    size 0 until the next layout pass, so a width read between the two is a
    race the fast machine always wins."""
    container = app._preview.active
    if container is None:
        return None
    return next((w for w in container.chunk_widgets.values() if w.size.width > 0), None)


@pytest.mark.asyncio
async def test_a_far_jump_mounts_a_capture_instead_of_building(
    tmp_path: Path, tmp_index_dir: Path
) -> None:
    index = wide_doc(tmp_path, tmp_index_dir)
    app = FNDApp(index_dir=index, initial_query="quartzfin")
    async with app.run_test(size=(100, 30)) as pilot:
        await wait_until(
            pilot,
            lambda: bool(app._search.groups) and app._preview.active is not None,
            timeout=20.0,
            message="preview never became active",
        )
        group = app._search.groups[0]
        seqs = sorted({h.chunk_seq for h in group.hits})
        assert len(seqs) >= 6, f"need several spread-out hits, got {seqs}"
        searcher = app._search.searcher
        assert searcher is not None
        chunks = searcher.get_file_chunks(group.parent_id)
        assert len(chunks) > tuning.FULLMOUNT_CHUNK_BUDGET, (
            f"fixture has {len(chunks)} chunks — inside the budget it is filled whole "
            "and there is no far jump left to serve"
        )

        target_seq = seqs[-1]
        store = app._preview.capture_store
        # The width captures are FILED under, which is the width chunks lay out
        # at — not the pane's content width, which does not shrink by the
        # scrollbar and so misses every capture once one is showing.
        pane_width = app._preview.capture_width(app.query_one("#preview_pane", VerticalScroll))

        await wait_until(
            pilot,
            lambda: (
                store.get(group.parent_id, app._search.query_signature(), pane_width, target_seq)
                is not None
            ),
            timeout=30.0,
            message="coverage never captured the far hit chunk",
        )

        app._preview.render_full_doc(group.parent_id, focus_chunk_seq=target_seq)
        await settle(pilot, ticks=10)

        active = app._preview.active
        assert active is not None
        widget = active.chunk_widgets.get(target_seq)
        assert isinstance(widget, FrozenChunkView), (
            f"jumped to chunk {target_seq} and got {type(widget).__name__} — the capture "
            "was not served, so the jump paid for a markdown build it did not need"
        )
        assert not isinstance(widget, FNDMarkdown)


TABLE_DOC = """## Section with a table

Some prose before the table mentioning quartzfin.

| Option | Notes | Extra |
| --- | --- | --- |
| `alpha` | a **quartzfin** cell | first |
| beta | plain text here | second |
| gamma | more content | third |

Prose after the table.
"""


class _CaptureHost(App[None]):
    CSS = """
    #pane { height: 100%; }
    .chunk-section { padding: 0 0 1 0; height: auto; }
    .chunk-first { padding: 1 0 0 0; }
    """

    _config = None

    @property
    def _effective_match_spec(self) -> MatchSpec:
        return MatchSpec.from_query("quartzfin")

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="pane"):
            yield FNDMarkdown(
                TABLE_DOC,
                match_spec=MatchSpec.from_query("quartzfin"),
                id="md",
                classes="chunk-section chunk-md-body chunk-first",
            )


@pytest.mark.asyncio
async def test_an_off_screen_capture_matches_the_on_screen_one() -> None:
    """Coverage builds on a screen the user never sees, and what it captures is
    served in place of the real widget — so any divergence between the two is a
    preview that renders differently depending on which path produced it.

    This caught tables coming back as an empty box: the off-screen screen is not
    current, so Textual will not lay it out on its own, and a DataTable sizes
    itself in response to its own posted refresh. Without a yield to the message
    pump between layout passes the capture holds the border and none of the
    cells — measured rows=3, size=0, virtual=0.
    """
    app = _CaptureHost()
    async with app.run_test(size=(90, 30)) as pilot:
        md = app.query_one("#md", FNDMarkdown)
        await md.build_done.wait()
        for _ in range(20):
            await pilot.pause()

        on_screen = freeze(md, chunk_seq=7)
        assert on_screen is not None, "on-screen freeze refused a laid-out chunk"
        assert "alpha" in "\n".join(s.text for s in on_screen.strips), "fixture has no table text"

        chunk = FileChunk(
            parent_id="p",
            path="wide.md",
            kind="md",
            page=0,
            slide=0,
            heading_path="",
            chunk_seq=7,
            blocks=[],
            body_md=TABLE_DOC,
        )
        # The warm host only reaches a handful of app attributes, all present
        # on this fixture; the cast is what lets a minimal host stand in.
        captured = await WarmHost(cast("FNDApp", app)).capture(
            chunk, 90, match_spec=MatchSpec.from_query("quartzfin")
        )
        assert captured is not None, "the warm host refused a chunk the pane captures fine"

        assert [s.text for s in captured.strips] == [s.text for s in on_screen.strips], (
            "an off-screen capture rendered differently from the on-screen one — "
            "it is served in place of the widget, so this is a visible difference"
        )


@pytest.mark.asyncio
async def test_coverage_does_not_hold_the_mount_in_flight(
    tmp_path: Path, tmp_index_dir: Path
) -> None:
    """Coverage must run as its own task, never inside the mount.

    ``lazy_mount.check`` bails while ``user_mount_in_flight()``, so awaiting
    coverage from the mount task means scrolling upward stops working for as
    long as coverage runs — tens of seconds on a large file. The mount is over
    when the preview is on screen; capturing ahead is separate work.
    """
    index = wide_doc(tmp_path, tmp_index_dir)
    app = FNDApp(index_dir=index, initial_query="quartzfin")
    async with app.run_test(size=(100, 30)) as pilot:
        await wait_until(
            pilot,
            lambda: bool(app._search.groups) and app._preview.active is not None,
            timeout=20.0,
            message="preview never became active",
        )
        await wait_until(
            pilot,
            lambda: (
                app._preview._coverage_task is not None
                and not app._preview._coverage_task.done()
                and not app._preview.user_mount_in_flight()
            ),
            timeout=30.0,
            message=(
                "coverage never ran with the mount already finished — it is being "
                "awaited by the mount task, which blocks lazy mount for its whole run"
            ),
        )


@pytest.mark.asyncio
async def test_coverage_stands_down_while_lazy_mount_runs(
    tmp_path: Path, tmp_index_dir: Path
) -> None:
    """Both are background mount work, and they must not overlap.

    Lazy mount's above-path awaits a SETTLED message pump before it can measure
    how far its prepend moved the anchor. Coverage feeds that pump continuously
    — a widget mounted and removed per capture — so running both at once left an
    upward scroll mounting nothing at all, which is a wall the user hits when
    scrolling back up through a file.
    """
    index = wide_doc(tmp_path, tmp_index_dir)
    app = FNDApp(index_dir=index, initial_query="quartzfin")
    async with app.run_test(size=(100, 30)) as pilot:
        await wait_until(
            pilot,
            lambda: bool(app._search.groups) and app._preview.active is not None,
            timeout=20.0,
            message="preview never became active",
        )
        presenter = app._preview
        group = app._search.groups[0]
        searcher = app._search.searcher
        assert searcher is not None
        chunks = searcher.get_file_chunks(group.parent_id)
        pane = app.query_one("#preview_pane", VerticalScroll)
        # The width captures are filed under, which is the width chunks lay out
        # at — the count below has to read the same key the capture writes.
        width = presenter.capture_width(pane)
        sig = app._search.query_signature()
        spec = app._effective_match_spec

        # Stop the presenter's OWN coverage run first. It may already be inside
        # a capture, and that capture completing would store a row this test
        # would read as the gate having failed — the background task is not what
        # is under test here.
        presenter.stop_background_work()
        for _ in range(6):
            await pilot.pause()

        # A lazy-mount batch that never finishes, so the only thing that can end
        # the wait is the check under test.
        blocker: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        app._lazy.task = asyncio.ensure_future(blocker)
        try:
            targets = [i for i, c in enumerate(chunks) if c.chunk_seq == group.hits[0].chunk_seq]
            assert targets, "need a hit chunk to try to capture"
            # A pass abandons itself when the cursor moves away from the anchor
            # it was planned around. Pass the CURRENT anchor, so that early exit
            # cannot stand in for the gate under test and pass this vacuously.
            before = presenter.capture_store.count(group.parent_id, sig, width)
            job = asyncio.create_task(
                presenter._capture_targets(
                    group.parent_id, sig, width, chunks, targets, spec, lambda: True
                )
            )
            for _ in range(12):
                await pilot.pause()
            assert presenter.capture_store.count(group.parent_id, sig, width) == before, (
                "coverage captured while a lazy-mount batch was in flight — the two "
                "compete for the same message pump"
            )
        finally:
            blocker.set_result(None)
            app._lazy.task = None
        # The positive control, and the whole reason this test means anything:
        # with lazy mount finished, the SAME call must capture. Without it, any
        # early return — a stale anchor, a changed query, an unrenderable chunk
        # — would satisfy the assertion above while proving nothing.
        assert await job > 0, (
            "the same targets captured nothing once lazy mount finished, so the "
            "assertion above proved only that some other guard fired"
        )


@pytest.mark.asyncio
async def test_the_freeze_sweep_yields_between_chunks(
    tmp_path: Path, tmp_index_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cold-to-warm swap must not hold the loop for the whole file.

    The sweep replaces every out-of-window chunk's widget tree with its capture.
    Done in one synchronous loop it is a hard stall of however long that takes —
    measured 424ms of worst-case loop block on 237 synthetic six-line chunks,
    and real chunks cost far more each. Sliced, the same run sits at parity with
    freezing switched off entirely (156ms against 191ms).

    Asserted as "other work ran while the sweep was in progress", which is the
    property that matters, rather than a wall-clock figure the suite cannot hold
    steady under load.
    """
    from fnd.tui.preview import frozen as frozen_mod

    # The sweep imports `freeze` from this module at call time, so this is the
    # binding it will use.
    real_freeze = frozen_mod.freeze

    swept: list[int] = []

    # Ticker turns seen at each freeze, so the yields can be counted BETWEEN the
    # first and last chunk rather than across the whole sweep.
    turns_at_freeze: list[int] = []

    def slow_freeze(chunk, chunk_seq):  # type: ignore[no-untyped-def]
        swept.append(chunk_seq)
        turns_at_freeze.append(len(gaps))
        # Longer than FREEZE_SLICE_SECONDS, so the slice budget is spent by every
        # single chunk and the sweep owes a yield at every boundary. Deriving the
        # owed count from elapsed time instead cannot work: the sweep's wall clock
        # includes the time it spends YIELDED, which grows under load while the
        # number of slice boundaries does not.
        time.sleep(tuning.FREEZE_SLICE_SECONDS + 0.004)
        return real_freeze(chunk, chunk_seq)

    monkeypatch.setattr(frozen_mod, "freeze", slow_freeze)

    index = wide_doc(tmp_path, tmp_index_dir)
    app = FNDApp(index_dir=index, initial_query="quartzfin")
    async with app.run_test(size=(100, 30)) as pilot:
        await wait_until(
            pilot,
            lambda: bool(app._search.groups) and app._preview.active is not None,
            timeout=30.0,
            message="preview never became active",
        )
        container = app._preview.active
        assert container is not None
        searcher = app._search.searcher
        assert searcher is not None
        chunks = searcher.get_file_chunks(container.parent_doc_id)
        mounted = sorted(container.mounted_indices)
        assert len(mounted) >= 8, f"need several mounted chunks to sweep, got {len(mounted)}"

        # The LONGEST gap between turns, not a count: the sweep awaits in its
        # prologue too, so counting turns passes whether or not the chunk loop
        # itself ever yields — which is exactly how the first version of this
        # test managed to be vacuous.
        gaps: list[float] = []

        async def ticker() -> None:
            last = time.perf_counter()
            while True:
                await asyncio.sleep(0)
                now = time.perf_counter()
                gaps.append(now - last)
                last = now

        beat = asyncio.create_task(ticker())
        await asyncio.sleep(0)
        gaps.clear()
        # Sweep everything: an empty window means every mounted chunk qualifies.
        sweep_started = time.perf_counter()
        await app._preview._freeze_chunks_outside_window(
            container, chunks, mounted[-1] + 1, mounted[-1] + 1
        )
        sweep_ms = (time.perf_counter() - sweep_started) * 1000
        # Let the ticker have a turn BEFORE cancelling it. A gap is only recorded
        # when the ticker next runs, so cancelling straight after the sweep threw
        # away the very measurement this test exists to take.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        beat.cancel()

    # Without this the test passes by sweeping NOTHING: the app freezes on its
    # own during startup, so by the time this runs the chunks can already be
    # stand-ins that the sweep skips.
    assert len(swept) >= 8, (
        f"the sweep only froze {len(swept)} chunks — too few to tell a blocking "
        "sweep from a yielding one, so this would pass either way"
    )
    worst_ms = max(gaps) * 1000 if gaps else 0.0
    # Turns the ticker got BETWEEN the first chunk and the last, which is the
    # only window the chunk loop controls. Counting across the whole sweep is
    # vacuous — the prologue awaits regardless — and the single worst gap cannot
    # tell a blocking sweep from one OS deschedule: it read 483ms against a
    # 249ms sweep on a Windows runner that was slicing correctly, a gap longer
    # than the sweep it was meant to describe.
    assert sweep_ms > 0, "the sweep took no measurable time; nothing was proven"
    assert len(turns_at_freeze) >= 8, "too few chunks swept to measure the cadence"
    turns_in_loop = turns_at_freeze[-1] - turns_at_freeze[0]
    # Every chunk overspends the slice budget, so the sweep owes one yield per
    # boundary. Two of slack: the first slice starts mid-prologue and the last
    # boundary has no chunk after it. `// 4` passed on two yields in eighty.
    owed = len(turns_at_freeze) - 2
    assert turns_in_loop >= owed, (
        "the freeze sweep held the loop through the whole swap — the "
        f"cold-to-warm transition is one uninterruptible block ({turns_in_loop} yields "
        f"against {owed} owed, {len(turns_at_freeze)} chunks, {sweep_ms:.0f}ms sweep, "
        f"worst gap {worst_ms:.0f}ms)"
    )


@pytest.mark.asyncio
async def test_lazy_mount_is_only_walled_off_until_first_paint(
    tmp_path: Path, tmp_index_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A revealed mount is doing housekeeping, and lazy mount must not wait.

    The mount task stays alive past the reveal to fill and freeze, and treating
    that tail as "a mount is happening" walls scroll-driven lazy mount off for
    seconds — scrolling up mounts nothing until it finishes. Slicing the sweep
    lengthened the tail and made it plain.

    The sweep is held open deliberately, because a fast tail would let this pass
    without proving anything.
    """
    from fnd.tui.preview.presenter import PreviewPresenter

    holding = asyncio.Event()

    async def slow_sweep(self, container, chunks, win_start, win_end):  # type: ignore[no-untyped-def]
        holding.set()
        await asyncio.sleep(3.0)

    monkeypatch.setattr(PreviewPresenter, "_freeze_chunks_outside_window", slow_sweep)

    index = wide_doc(tmp_path, tmp_index_dir)
    app = FNDApp(index_dir=index, initial_query="quartzfin")
    async with app.run_test(size=(100, 30)) as pilot:
        await wait_until(
            pilot,
            lambda: holding.is_set(),
            timeout=30.0,
            message="the mount never reached the freeze sweep",
        )
        presenter = app._preview
        assert presenter.user_mount_in_flight(), "sweep is held, so the mount is still running"
        # The reveal is scheduled, not synchronous, so it can land AFTER the
        # backfill has begun. Wait for it rather than sampling once: what is
        # under test is that a PAINTED mount stops walling lazy mount off, not
        # how many frames the reveal took to arrive.
        await wait_until(
            pilot,
            lambda: not presenter.mount_before_first_paint(),
            timeout=10.0,
            message=(
                "the container never registered as painted while the backfill "
                "ran — lazy mount stays walled off for the whole of it"
            ),
        )
        assert presenter.user_mount_in_flight(), "the sweep should still be held"


@pytest.mark.asyncio
async def test_coverage_skips_chunks_too_expensive_to_build(
    tmp_path: Path, tmp_index_dir: Path
) -> None:
    """A capture runs on the UI's event loop, so an expensive one IS a freeze.

    The duty cycle between captures cannot help — it has no way into a single
    build. Measured on a real PDF, one 120,123-character chunk took 4.4s to
    build and produced an 8.4s freeze in a live session, against a 5.3ms median.
    So the outliers must be refused BEFORE building, which is the only point at
    which the cost is still avoidable.
    """
    index = wide_doc(tmp_path, tmp_index_dir)
    app = FNDApp(index_dir=index, initial_query="quartzfin")
    async with app.run_test(size=(100, 30)) as pilot:
        await wait_until(
            pilot,
            lambda: bool(app._search.groups) and app._preview.active is not None,
            timeout=20.0,
            message="preview never became active",
        )
        presenter = app._preview
        group = app._search.groups[0]
        searcher = app._search.searcher
        assert searcher is not None
        chunks = searcher.get_file_chunks(group.parent_id)
        pane = app.query_one("#preview_pane", VerticalScroll)
        width = presenter.capture_width(pane)
        sig = app._search.query_signature()

        presenter.stop_background_work()
        for _ in range(6):
            await pilot.pause()

        targets = [i for i, c in enumerate(chunks) if c.chunk_seq == group.hits[0].chunk_seq]
        assert targets, "need a hit chunk"
        victim_index = targets[0]

        # Positive control FIRST: this chunk is capturable as it stands, so a
        # miss after the guard is the guard and not some unrelated bail-out.
        before = presenter.capture_store.count(group.parent_id, sig, width)
        assert (
            await presenter._capture_targets(
                group.parent_id,
                sig,
                width,
                chunks,
                targets,
                app._effective_match_spec,
                lambda: True,
            )
            > 0
        ), "the chunk was not capturable to begin with; this test would prove nothing"
        presenter.capture_store.drop_file(group.parent_id)

        # Now make it oversized and confirm coverage refuses it. FileChunk is
        # frozen, so this is a copy standing in the same position.
        # A FIXED size, matching the 120K-character chunk measured on the real
        # PDF — deliberately not derived from the threshold, because a body
        # sized as a multiple of the limit scales with it and no change to the
        # limit could ever fail this test.
        chunks[victim_index] = dataclasses.replace(chunks[victim_index], body_md="x " * 60_000)
        before = presenter.capture_store.count(group.parent_id, sig, width)
        captured = await presenter._capture_targets(
            group.parent_id,
            sig,
            width,
            chunks,
            targets,
            app._effective_match_spec,
            lambda: True,
        )
        assert captured == 0, "coverage built a chunk far over the cost threshold"
        assert presenter.capture_store.count(group.parent_id, sig, width) == before


@pytest.mark.asyncio
async def test_a_capture_is_cut_at_the_width_it_will_be_served_into(
    tmp_path: Path, tmp_index_dir: Path
) -> None:
    """A capture must be built at the width the chunk actually lays out at.

    Children lay out inside the pane's SCROLLABLE content region, which the
    vertical scrollbar shrinks by a column; `size` and `content_size` are equal
    to each other and neither moves with the bar. Building at those meant that
    for any document long enough to need a scrollbar — which is every document
    this feature exists for — the capture was a column wider than the slot it
    was served into, so the last cell of every row was cropped and the strips
    were wrapped for a width never displayed. Heights, `first_match_row` and the
    stop rows were all off by the difference.

    The earlier version of this test monkeypatched the width function and then
    asserted the writer and the sweep agreed — which they did by construction,
    whatever the function returned. It passed with the bug present.
    """
    index = wide_doc(tmp_path, tmp_index_dir)
    app = FNDApp(index_dir=index, initial_query="quartzfin")
    async with app.run_test(size=(100, 30)) as pilot:
        # `active` is the CONTAINER existing; its children are size-zero until
        # layout runs, and every assertion below is about a laid-out width.
        # Windows reported "no mounted chunk to measure against" on that gap.
        await wait_until(
            pilot,
            lambda: bool(app._search.groups) and _laid_out_chunk(app) is not None,
            timeout=20.0,
            message="no chunk in the preview was ever laid out",
        )
        presenter = app._preview
        pane = app.query_one("#preview_pane", VerticalScroll)
        container = presenter.active
        assert container is not None

        # This only means anything while a scrollbar is up, because that is the
        # only time the widths diverge. Refuse to pass quietly otherwise.
        assert pane.show_vertical_scrollbar, (
            "no scrollbar in this fixture, so every pane width coincides and "
            "this test cannot tell a correct capture width from a wrong one"
        )
        assert presenter.capture_width(pane) != pane.content_size.width

        # The width a real mounted chunk was laid out at is the ground truth.
        live = _laid_out_chunk(app)
        assert live is not None, "no mounted chunk to measure against"
        assert presenter.capture_width(pane) == live.size.width, (
            f"captures would be cut at {presenter.capture_width(pane)} while chunks "
            f"lay out at {live.size.width} — every served row loses its last cells"
        )


@pytest.mark.asyncio
async def test_a_tall_chunk_is_captured_at_the_width_it_was_asked_for(
    tmp_path: Path, tmp_index_dir: Path
) -> None:
    """The off-screen jig must not reserve a scrollbar column of its own.

    `WarmHost` lays its screen out `_LAYOUT_HEIGHT` rows tall. A chunk taller
    than that overflows the container, which then grows a vertical scrollbar, and
    the chunk lays out one column narrower than the width asked for — so the
    capture is cut for a width it is not filed under, and every row it is served
    into is a column short. Measured before the fix, the boundary was exactly the
    layout box: 300 rendered rows captured at 76, 420 rows at 75.

    Nothing else detects this. The pane-width test compares the FILING width
    against a mounted chunk; this compares what came back from the jig against
    what was requested, which is the other half.
    """
    index = wide_doc(tmp_path, tmp_index_dir)
    app = FNDApp(index_dir=index, initial_query="quartzfin")
    async with app.run_test(size=(100, 30)) as pilot:
        await wait_until(
            pilot,
            lambda: bool(app._search.groups) and app._preview.active is not None,
            timeout=20.0,
            message="preview never became active",
        )
        presenter = app._preview
        presenter.stop_background_work()
        for _ in range(4):
            await pilot.pause()

        group = app._search.groups[0]
        searcher = app._search.searcher
        assert searcher is not None
        chunk = searcher.get_file_chunks(group.parent_id)[0]
        # Force the chunk past the jig's layout box so it would overflow.
        tall = dataclasses.replace(
            chunk, body_md="\n\n".join(f"paragraph {i} quartzfin" for i in range(260))
        )
        requested = 76
        capture = await presenter._warm_host.capture(
            tall, requested, match_spec=app._effective_match_spec
        )
        assert capture is not None, "the jig produced nothing to check"
        assert capture.outer_height > warm_host_mod._LAYOUT_HEIGHT, (
            f"chunk rendered {capture.outer_height} rows against a layout box of "
            f"{warm_host_mod._LAYOUT_HEIGHT}, so it never overflowed and this test "
            f"cannot detect the bug"
        )
        assert capture.width == requested, (
            f"asked for {requested} columns and got {capture.width}: the off-screen "
            f"container grew a scrollbar, so the capture is cut for a width it will "
            f"not be displayed at"
        )


@pytest.mark.asyncio
async def test_a_resize_does_not_leave_frozen_chunks_painting_cropped_text(
    tmp_path: Path, tmp_index_dir: Path
) -> None:
    """Strips are width-locked, and `render_line` crops them to the widget.

    So a narrower pane does not re-wrap a frozen chunk — it removes the
    right-hand cells of every row, and the text is simply gone until something
    rebuilds the file. Measured before the fix on a 120-section document,
    shrinking 100 to 80 columns left 87 chunks rendering 20 columns short.

    The store sweep cannot help: those strips are on screen, not in the store.
    This asserts the on-screen half — that no frozen chunk is left painting
    strips cut for a width it is no longer laid out at.
    """
    index = wide_doc(tmp_path, tmp_index_dir)
    app = FNDApp(index_dir=index, initial_query="quartzfin")
    async with app.run_test(size=(100, 30)) as pilot:
        await wait_until(
            pilot,
            lambda: bool(app._search.groups) and app._preview.active is not None,
            timeout=20.0,
            message="preview never became active",
        )
        presenter = app._preview

        def frozen_views() -> list[FrozenChunkView]:
            container = presenter.active
            if container is None:
                return []
            return [w for w in container.chunk_widgets.values() if isinstance(w, FrozenChunkView)]

        # Freeze one mounted chunk by hand rather than waiting for the
        # background sweep: the sweep's timing is not what is under test, and
        # depending on it makes this a race. This is exactly what the sweep does
        # — capture the widget, mount the capture in its place.
        container = presenter.active
        assert container is not None
        # Ask `freeze` itself which chunk is ready, rather than predicting its
        # answer. Geometry is only one of its four refusal reasons: it also
        # declines a hidden ancestor, an ancestor still at `-pre-reveal` opacity,
        # and an unlaid table. Selecting on `size > 0` alone picked chunks it
        # then refused, and the assertion below failed on the slowest CI runner
        # while passing on the other two — the container had not been revealed
        # yet, which the "preview is active" wait above does not cover.
        picked: list[tuple[int, FNDMarkdown, FrozenChunk]] = []

        def _capturable() -> bool:
            live = presenter.active
            if live is None:
                return False
            for seq, w in list(live.chunk_widgets.items()):
                if not isinstance(w, FNDMarkdown):
                    continue
                captured = freeze(w, seq)
                if captured is not None:
                    picked.append((seq, w, captured))
                    return True
            return False

        await wait_until(
            pilot,
            _capturable,
            timeout=20.0,
            message="no mounted chunk ever became capturable",
        )
        seq, widget, captured = picked[0]
        view = FrozenChunkView(captured)
        await container.mount(view, before=widget)
        container.chunk_widgets[seq] = view
        widget.remove()
        for _ in range(4):
            await pilot.pause()
        assert frozen_views(), "the frozen chunk did not stay mounted"

        def repaired_in_place() -> bool:
            views = frozen_views()
            # The emptiness check is part of the CONDITION, not an assert:
            # `wait_until` swallows a raising predicate, so an assert here would
            # be invisible and the timeout would report the wrong diagnosis. An
            # empty list is not "nothing stale" — if the repair ever removed the
            # view instead of re-cutting it, `any()` over nothing is False and
            # this would pass for the wrong reason.
            return bool(views) and not any(
                v.size.width > 0 and v.frozen.width != v.size.width for v in views
            )

        container_before = presenter.active
        await pilot.resize_terminal(80, 30)
        await wait_until(
            pilot,
            lambda: presenter.active is not None and repaired_in_place(),
            # Generous on purpose. The repair is deliberately unhurried — it
            # debounces the gesture, waits out any mount in flight, and yields
            # between captures — so under full-suite load it legitimately takes
            # far longer than it does alone. The assertion is that it happens,
            # not that it happens fast.
            timeout=90.0,
            message=(
                "frozen chunks are still painting strips cut for the old width, "
                "or the view was removed instead of repaired in place"
            ),
        )

        # Repaired IN PLACE. Replacing the container instead is what blanked the
        # pane for 90ms and made one drag cost five full rebuilds.
        assert presenter.active is container_before, (
            "the repair swapped the container instead of re-cutting its strips"
        )

        stale = [
            (v.frozen.width, v.size.width)
            for v in frozen_views()
            if v.size.width > 0 and v.frozen.width != v.size.width
        ]
        assert not stale, (
            f"{len(stale)} frozen chunks are cut for a width they are not laid out "
            f"at {stale[:3]} — every row of those loses its right-hand cells"
        )


@pytest.mark.asyncio
async def test_the_repair_re_arms_but_cannot_chain_forever(
    tmp_path: Path, tmp_index_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two halves of one guarantee, and a capture that always fails proves both.

    A pass abandons whenever the width moves again mid-drag, and reports arriving
    while a pass runs are dropped — so the pass has to re-arm itself or the tail
    stays cropped until the next navigation. Gating that re-arm on having
    repaired something looks safer but loses exactly the case it is needed for: a
    pass that abandons BEFORE its first success.

    Removing the gate then needs a different termination guarantee, because a
    chunk whose capture keeps failing would re-arm forever. That is the bound.

    With every capture failing, `repaired` is always zero: a `repaired`-gated
    re-arm gives one pass, and an unbounded one never stops. The correct
    behaviour is exactly `STALE_STRIP_MAX_PASSES`.
    """
    index = wide_doc(tmp_path, tmp_index_dir)
    app = FNDApp(index_dir=index, initial_query="quartzfin")
    async with app.run_test(size=(100, 30)) as pilot:
        await wait_until(
            pilot,
            lambda: bool(app._search.groups) and app._preview.active is not None,
            timeout=20.0,
            message="preview never became active",
        )
        presenter = app._preview
        # Same gap as above: wait for a chunk that has been through layout
        # before stopping the work that would produce one.
        await wait_until(
            pilot,
            lambda: _laid_out_chunk(app) is not None,
            timeout=20.0,
            message="no chunk in the preview was ever laid out",
        )
        presenter.stop_background_work()
        for _ in range(4):
            await pilot.pause()
        container = presenter.active
        assert container is not None

        live = next(
            (
                (seq, w)
                for seq, w in container.chunk_widgets.items()
                if isinstance(w, FNDMarkdown) and w.size.width > 0 and w.size.height > 0
            ),
            None,
        )
        assert live is not None, "no laid-out chunk to freeze"
        seq, widget = live
        captured = freeze(widget, seq)
        assert captured is not None
        view = FrozenChunkView(captured)
        await container.mount(view, before=widget)
        container.chunk_widgets[seq] = view
        widget.remove()
        for _ in range(4):
            await pilot.pause()

        # Force the strips stale without a real resize, so the pass has work.
        object.__setattr__(view.frozen, "width", view.size.width + 7)

        async def always_fails(*_args: object, **_kwargs: object) -> None:
            return None

        monkeypatch.setattr(presenter._warm_host, "capture", always_fails)
        # And empty the store, or a hit would satisfy the repair without ever
        # reaching the failing builder.
        presenter.capture_store.clear()

        passes = 0
        original = presenter._repair_stale_strips

        async def counting(attempt: int = 0) -> None:
            nonlocal passes
            passes += 1
            await original(attempt)

        monkeypatch.setattr(presenter, "_repair_stale_strips", counting)

        presenter.on_stale_strips()
        await wait_until(
            pilot,
            lambda: (
                presenter._stale_strip_repair is not None and presenter._stale_strip_repair.done()
            ),
            timeout=60.0,
            message="the repair chain never finished",
        )
        for _ in range(10):
            await pilot.pause()

        assert passes > 1, (
            "the repair ran once and stopped even though it had repaired nothing — "
            "a pass that abandons before its first success must still re-arm"
        )
        assert passes <= tuning.STALE_STRIP_MAX_PASSES, (
            f"the repair chained {passes} times against a bound of "
            f"{tuning.STALE_STRIP_MAX_PASSES}; with every capture failing it would "
            f"re-arm forever"
        )


@pytest.mark.asyncio
async def test_a_failed_mount_does_not_disable_warming_for_the_session() -> None:
    """`ensure` installs its screen under a FIXED name.

    So a first call that installs the screen and then fails to mount into it
    leaves that name taken. Re-installing it on the retry raises on the
    duplicate, the raise is swallowed, and `ensure` returns `None` for the rest
    of the session — every capture missing, nothing logged, and the only symptom
    a preview that is quietly slow forever.
    """
    app = _CaptureHost()
    async with app.run_test(size=(90, 30)):
        host = WarmHost(cast("FNDApp", app))

        calls = {"n": 0}

        async def failing_mount(*args: object, **kwargs: object) -> None:
            calls["n"] += 1
            raise RuntimeError("mount refused")

        # Fail the FIRST mount only, exactly as a transient teardown race would.
        import textual.screen

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(textual.screen.Screen, "mount", failing_mount)
            assert await host.ensure() is None, "the failing mount should yield no container"
        # The context restored the real mount on exit — so this second call is
        # the retry, and it is the whole test.
        assert calls["n"] == 1, "the mount failed more than the once this models"

        assert await host.ensure() is not None, (
            "warming stayed dead after one failed mount — the screen name was "
            "still taken and every retry raised on the duplicate"
        )


# --- Tier order under a moving cursor -------------------------------------
#
# What a pass DOES with the cursor is only visible in the order files are
# worked, and that order is decided in `_run_coverage` from the anchor, the
# navigation order and the store — none of which need a real corpus, a real
# capture, or a real widget. Driving the real method against stubbed edges
# gives the trace directly; a full app test would have to infer the order from
# store contents after the fact, and could not tell "covered late" from
# "covered and evicted".


@dataclasses.dataclass
class _StubChunk:
    chunk_seq: int
    # Enough for `uses_markdown_renderer` to accept it. Coverage skips any
    # chunk the off-screen builder cannot build, so a stub without these is
    # uncapturable and every file in the plan gets silently skipped — the
    # tests below would then assert about an ordering that never happened.
    kind: str = "md"
    body_md: str = "stub body"


class _StubSession:
    """Enough ProgressSession for a whole-file warm to report into.

    ``closed`` is modelled because the presenter reads it to decide whether its
    session was retired under it, and ``close`` because a stub without one turns
    every retirement into a suppressed AttributeError.
    """

    def __init__(self) -> None:
        self.closed = False
        self.abandoned = False
        self.reports: list[tuple[float, float]] = []
        #: Reports that arrived after retirement — the real session silently
        #: drops these, so nothing else could show they were being sent.
        self.reports_after_close: list[tuple[float, float]] = []

    def enter(self, _phase: str) -> None: ...

    def report(self, done: float, total: float) -> None:
        target = self.reports_after_close if self.closed else self.reports
        target.append((done, total))

    def close(self) -> None:
        self.closed = True

    def abandon(self) -> None:
        """Retire without claiming the work finished."""
        self.closed = True
        self.abandoned = True


class _StubProgress:
    """The facility's single AMBIENT slot, which the warm must not steal.

    ``begin`` takes the slot and retires whoever held it, as the real facility
    does — a stub that merely handed back a session could not model the
    collision these tests exist to pin.
    """

    def __init__(self) -> None:
        self.ambient: _StubSession | None = None
        self.sessions: list[_StubSession] = []

    def begin(self, _plan: object, *, label: str = "") -> _StubSession:
        if self.ambient is not None:
            self.ambient.close()
        session = _StubSession()
        self.ambient = session
        self.sessions.append(session)
        return session


class _StubCoverageApp:
    """Just enough app for `_run_coverage` to run its plan."""

    def __init__(self) -> None:
        # No config, so coverage falls back to the tuning defaults.
        self._config = None
        self._progress = _StubProgress()
        self._preview_scroll = cast("object", type("_S", (), {"is_settling": False})())
        self._search = cast("object", type("_Q", (), {"query_signature": lambda self: "sig"})())
        self._effective_match_spec = MatchSpec()
        self._lazy = type("_L", (), {"task": None})()

    def query_one(self, *_args: object, **_kwargs: object) -> object:
        return object()


class _CoverageTrace:
    """A presenter wired to record which file each tier works, and for how long.

    Captures are stubbed to a fixed sleep so the trace is about ORDER, not
    speed: the real host is serial at ~10 chunks a second, which is exactly why
    order is the whole design (see `fnd.tui.preview.coverage`).
    """

    def __init__(
        self,
        ids: list[str],
        *,
        chunks_per_file: int = 20,
        hits_by_file: dict[str, list[int]] | None = None,
    ) -> None:
        from fnd.tui.preview.presenter import PreviewPresenter

        self.ids = ids
        self.events: list[tuple[str, str, int]] = []
        self.warming_seen: list[str | None] = []
        # A gate, not a sleep. Coverage's own pace is what these tests are
        # about, so a fixed wait for "tier 1 has started" is a precondition
        # weaker than the code it guards: hits-first finishes a stub file's
        # three hit captures in ~15ms, and a 30ms wait then lands in the NEXT
        # file and asserts the setup it meant to establish had failed.
        self.covered: set[str] = set()
        self.pause_after: tuple[str, int] | None = None
        self.paused = asyncio.Event()
        self.resume = asyncio.Event()
        p = cast("Any", PreviewPresenter.__new__(PreviewPresenter))
        p._app = _StubCoverageApp()
        # The scheduler's own state, from the product's initialiser rather than
        # mirrored here: it grows, and a copy goes stale without saying so.
        p.init_coverage_state()
        # Real code reads this on the warm's completion path. Left unset, that
        # path raised AttributeError into `_run_coverage`'s blanket except and
        # the tests passed without ever exercising it.
        p.active = None
        p.chunk_cache = {}
        # `count` as well as `total_rows`: the whole-file warm reads it to
        # place the progress line, and a stub missing it raises into
        # `_run_coverage`'s blanket except, which reads as "nothing to do".
        p.capture_store = type(
            "_S",
            (),
            {"total_rows": lambda self: 0, "count": lambda self, *_a, **_k: 0},
        )()
        p.capture_width = lambda _pane: 80
        p.diag_log = lambda _msg: None
        p.navigation_order = lambda: [(f, 0) for f in ids]
        # Honest, because preemption reads it: background work stands down
        # while the CURSOR's file still needs covering. A stub that answers
        # "yes" forever starves every neighbour and seed file, which is the
        # stub being unreal rather than the product being wrong.
        p._file_needs_coverage = lambda pid, *_a: pid not in self.covered
        p._landing_index = lambda *_a: 0
        hits = hits_by_file or {}
        p.hit_indices = lambda pid, _chunks: hits.get(pid, [0, 5, 10])
        p._held_indices = lambda *_a: set()

        async def _chunks(_pid: str) -> list[_StubChunk]:
            return [_StubChunk(i) for i in range(chunks_per_file)]

        p._coverage_chunks = _chunks
        p._capture_file_targets = self._record
        self.presenter = p

    async def _record(
        self,
        parent_id: str,
        _query_sig: str,
        _width: int,
        _chunks: list[_StubChunk],
        targets: list[int],
        _spec: MatchSpec,
        still_wanted: Any,
        on_capture: Any = None,
    ) -> int:
        done = 0
        for _ in targets:
            if not still_wanted():
                self.events.append(("abandoned", parent_id, done))
                return done
            await asyncio.sleep(0.005)
            done += 1
            if on_capture is not None:
                # (walked, captured) — the stub captures everything it walks.
                on_capture(done, done)
            if self.pause_after == (parent_id, done):
                # Hold this file mid-capture so the test can move the cursor at
                # a known point, then let it go.
                self.paused.set()
                await self.resume.wait()
        self.events.append(("covered", parent_id, done))
        self.covered.add(parent_id)
        return done

    def files_worked(self) -> list[str]:
        """Files that actually got captures, in the order they got them."""
        out: list[str] = []
        for kind, pid, n in self.events:
            if kind == "covered" or n:
                if not out or out[-1] != pid:
                    out.append(pid)
        return out


async def _drain(trace: _CoverageTrace, *, limit: float = 8.0) -> None:
    task = trace.presenter._coverage_task
    assert task is not None
    deadline = time.perf_counter() + limit
    while not task.done() and time.perf_counter() < deadline:
        await asyncio.sleep(0.01)
    if not task.done():
        task.cancel()
        with contextlib.suppress(BaseException):
            await task
        pytest.fail(f"the coverage pass did not finish within {limit}s")


@pytest.mark.asyncio
async def test_moving_within_a_file_keeps_warming_that_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The file the user is IN must not be shunted behind its neighbours.

    Tier 1's order is nearest-first around the cursor, so a move inside the file
    does invalidate the ORDER — but not the file, which is still the only one
    that can serve the next keypress. Abandoning the pass on that basis handed
    the serial host to the neighbours for the rest of it: the file was dropped
    part-covered, both neighbours were covered whole, and it was only picked up
    a pass later. The results arrow reported it faithfully, going from warming
    to cold the moment the neighbour's first capture started.
    """
    monkeypatch.setattr(tuning, "PREVIEW_WARM_DELAY", 0.0)
    trace = _CoverageTrace(["A", "B", "C", "D", "E"])
    p = trace.presenter
    trace.pause_after = ("A", 1)
    p.start_coverage("A", 0)
    # Hold tier 1 mid-file, then move to another match IN THE SAME FILE.
    await asyncio.wait_for(trace.paused.wait(), timeout=5.0)
    assert p.coverage_parent == "A", "setup: tier 1 should be on the current file"
    p.start_coverage("A", 10)
    # Checked with no await in between, so no event-loop turn has passed: the
    # marker must survive the move itself. Sleeping first would race tier 1
    # finishing this stub file's three captures, which is a question about the
    # fixture's speed rather than about the marker.
    assert p.coverage_parent == "A", (
        "the file the cursor is in stopped being warmed the moment the cursor "
        "moved inside it — the results arrow paints this as cold"
    )
    trace.resume.set()
    await _drain(trace)
    worked = trace.files_worked()
    assert worked[0] == "A", f"expected the current file first, got {worked}"
    assert worked.index("A") < worked.index("B"), (
        f"the current file was covered after its neighbours: {worked}"
    )
    covered = [pid for kind, pid, _n in trace.events if kind == "covered"]
    assert covered.index("A") < covered.index("B"), (
        f"the current file finished after a neighbour: {trace.events}"
    )


@pytest.mark.asyncio
async def test_leaving_a_file_stands_its_coverage_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Re-planning in place must not become "never let go".

    Tier 1 following the cursor WITHIN a file is the fix above; tier 1 holding
    on after the cursor has left it would be a worse bug than the one it
    replaces, because the pass planned around the new current file then queues
    behind a whole pass for a file nobody is reading.

    A property guard, not a red-then-green test: two things enforce this — the
    per-round predicate is bound to the file, and the loop ends when the cursor
    stops moving within it — so removing either one alone still passes. What it
    pins is the outcome, which is what a later change to either would break.
    """
    monkeypatch.setattr(tuning, "PREVIEW_WARM_DELAY", 0.0)
    trace = _CoverageTrace(["A", "B", "C", "D", "E"])
    p = trace.presenter
    trace.pause_after = ("A", 1)
    p.start_coverage("A", 0)
    await asyncio.wait_for(trace.paused.wait(), timeout=5.0)
    # A different file AND a different chunk: leaving on the same chunk seq
    # would be stood down by the "cursor stopped moving" test alone, so it
    # cannot tell whether the file is being checked at all.
    p.start_coverage("C", 10)
    trace.resume.set()
    await asyncio.sleep(0.05)

    assert p.coverage_parent != "A", (
        "coverage stayed on the file the cursor had left; the new current "
        "file now waits behind a whole pass of work nobody asked for"
    )
    await _drain(trace)
    abandoned = [pid for kind, pid, _n in trace.events if kind == "abandoned"]
    assert "A" in abandoned, f"the departed file should have stood down: {trace.events}"


@pytest.mark.asyncio
async def test_the_seed_files_are_actually_covered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The head of the result list is seeded, not just planned.

    The staleness test a file is judged by has to be built from the same parts
    as the plan it is judged against. Judging seed files by the neighbour window
    alone made the whole tier inert: a seed file outside that window is never
    "still wanted", so every one of them paid a full off-loop chunk decode and
    was then abandoned before its first capture — with a stationary cursor as
    much as a moving one.
    """
    monkeypatch.setattr(tuning, "PREVIEW_WARM_DELAY", 0.0)
    monkeypatch.setattr(tuning, "COVERAGE_NEIGHBOUR_FILES", 1)
    monkeypatch.setattr(tuning, "COVERAGE_SEED_FILES", 4)
    trace = _CoverageTrace(["A", "B", "C", "D", "E"])
    trace.presenter.start_coverage("A", 0)
    await _drain(trace)
    covered = {pid for kind, pid, _n in trace.events if kind == "covered"}
    # C and D are seed files only: with a span of 1 the neighbours of A are B
    # alone, so nothing but the seed tier can reach them.
    assert {"C", "D"} <= covered, (
        f"the seed tier captured nothing for files outside the neighbour window: {trace.events}"
    )


@pytest.mark.asyncio
async def test_context_work_yields_to_a_file_the_cursor_moved_towards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Margins are context; a hit is what a jump lands on.

    A pass is only re-planned when it ENDS, and the plan is the only place a
    newly neighbouring file can appear — so a margin walk that runs to
    completion holds off every landing the cursor has just moved towards. The
    walk could not be stood down either: its test was plan membership, and the
    seed is in every plan. Measured on the real index, the file two below the
    cursor waited 31.0s for its first capture, 26.3s of it the previous plan's
    margins over files already covered.
    """
    monkeypatch.setattr(tuning, "PREVIEW_WARM_DELAY", 0.0)
    monkeypatch.setattr(tuning, "COVERAGE_NEIGHBOUR_FILES", 2)
    monkeypatch.setattr(tuning, "COVERAGE_SEED_FILES", 3)
    trace = _CoverageTrace(["A", "B", "C", "D", "E", "F"])
    p = trace.presenter
    # Only a MARGIN call has this many targets: the stub's three hits give a
    # three-target list on the hits walk and sixteen on the margin walk.
    trace.pause_after = ("B", 4)
    p.start_coverage("A", 0)
    await asyncio.wait_for(trace.paused.wait(), timeout=5.0)
    # E is outside the running plan (A, B, C) and has nothing captured.
    p.start_coverage("E", 0)
    trace.resume.set()
    await _drain(trace)

    abandoned = [pid for kind, pid, _n in trace.events if kind == "abandoned"]
    assert "B" in abandoned, (
        f"margin work carried on while a file the cursor moved towards had "
        f"nothing to land on: {trace.events}"
    )
    covered = {pid for kind, pid, _n in trace.events if kind == "covered"}
    assert "E" in covered, f"the file the cursor moved to was never covered: {trace.events}"


@pytest.mark.asyncio
async def test_a_request_arriving_late_in_a_pass_is_still_served(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pass checks for a request once, before its margin walk.

    So a request that lands DURING that walk has already been missed, and the
    cursor has not moved — the loop would call itself finished and the file
    would never warm. The outstanding request is what keeps it going.
    """
    monkeypatch.setattr(tuning, "PREVIEW_WARM_DELAY", 0.0)
    monkeypatch.setattr(tuning, "COVERAGE_NEIGHBOUR_FILES", 2)
    monkeypatch.setattr(tuning, "COVERAGE_SEED_FILES", 3)
    trace = _CoverageTrace(["A", "B", "C"], chunks_per_file=20)
    p = trace.presenter
    # Only a margin call is this long, so this pauses after the request check.
    trace.pause_after = ("B", 4)
    p.start_coverage("A", 0)
    await asyncio.wait_for(trace.paused.wait(), timeout=5.0)
    p.request_full_warm("A", 0)
    trace.resume.set()
    await _drain(trace)

    whole = [e for e in trace.events if e[0] == "covered" and e[1] == "A" and e[2] == 20]
    assert whole, f"the requested file was never warmed whole: {trace.events}"
    assert not p.full_warm_in_progress, "a finished warm should retire its request"


@pytest.mark.asyncio
async def test_context_work_stands_down_for_a_requested_warm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Margins are background context; a requested warm is being waited on."""
    monkeypatch.setattr(tuning, "PREVIEW_WARM_DELAY", 0.0)
    monkeypatch.setattr(tuning, "COVERAGE_NEIGHBOUR_FILES", 2)
    monkeypatch.setattr(tuning, "COVERAGE_SEED_FILES", 3)
    trace = _CoverageTrace(["A", "B", "C"])
    p = trace.presenter
    trace.pause_after = ("B", 4)  # only a margin call has this many targets
    p.start_coverage("A", 0)
    await asyncio.wait_for(trace.paused.wait(), timeout=5.0)
    p.request_full_warm("A", 0)
    trace.resume.set()
    await _drain(trace)

    abandoned = [pid for kind, pid, _n in trace.events if kind == "abandoned"]
    assert "B" in abandoned, (
        f"margin work carried on while the user waited on a whole-file warm: {trace.events}"
    )


@pytest.mark.asyncio
async def test_cancelling_only_stops_the_file_it_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The key toggles per FILE, and the caller needs to know which it did.

    Unnamed, pressing it on a second file stopped the warm on the first and
    started nothing — the one press that could not do what it looked like.
    """
    monkeypatch.setattr(tuning, "PREVIEW_WARM_DELAY", 0.0)
    trace = _CoverageTrace(["A", "B"])
    p = trace.presenter
    assert p.cancel_full_warm("A") is False, "nothing running yet"
    p.request_full_warm("A", 0)
    assert p.cancel_full_warm("B") is False, "a request for another file is not this one"
    assert p.full_warm_in_progress, "cancelling B must leave A's warm running"
    assert p.cancel_full_warm("A") is True
    assert p.cancel_full_warm("A") is False
    await _drain(trace)


@pytest.mark.asyncio
async def test_a_finished_warm_closes_its_progress_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One line per request, retired when the request is."""
    monkeypatch.setattr(tuning, "PREVIEW_WARM_DELAY", 0.0)
    trace = _CoverageTrace(["A", "B"])
    p = trace.presenter
    p.request_full_warm("A", 0)
    await _drain(trace)
    progress = p._app._progress
    assert progress.sessions, "the warm never opened a line"
    assert all(s.closed for s in progress.sessions), (
        "a finished warm left its progress line open, holding the ambient slot"
    )


@pytest.mark.asyncio
async def test_a_warm_never_takes_the_line_from_an_ambient_operation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The facility has ONE ambient slot and ``begin`` retires whatever holds it.

    Indexing is the other ambient operation and can run for minutes, so a warm
    that opened unconditionally left the machine indexing with no indication —
    the exact regression the index line exists to prevent.
    """
    monkeypatch.setattr(tuning, "PREVIEW_WARM_DELAY", 0.0)
    trace = _CoverageTrace(["A", "B"])
    p = trace.presenter
    progress = p._app._progress
    indexing = _StubSession()
    progress.ambient = indexing

    p.request_full_warm("A", 0)
    await _drain(trace)
    assert not indexing.closed, "the warm evicted the index's progress line"
    assert not progress.sessions, "the warm opened a line while the slot was taken"


@pytest.mark.asyncio
async def test_warming_a_second_file_does_not_inherit_the_first_ones_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The first walk stands down without finishing, so nothing retires its
    line; reusing it would report the second file's counts under the first
    file's name, and file one calibration sample spanning two files."""
    monkeypatch.setattr(tuning, "PREVIEW_WARM_DELAY", 0.0)
    trace = _CoverageTrace(["A", "B"])
    p = trace.presenter
    # Inside the WHOLE-FILE walk, not the hits walk: only it runs past three
    # targets for this file, and only it has opened a line by then.
    trace.pause_after = ("A", 4)
    p.request_full_warm("A", 0)
    await asyncio.wait_for(trace.paused.wait(), timeout=5.0)
    first = list(p._app._progress.sessions)
    assert first, "setup: the first warm should have opened a line"

    p.request_full_warm("B", 0)
    # Checked at the moment of the switch, with no await in between. Later, the
    # same session object is closed when B's warm finishes — so a test that
    # looked afterwards could not tell reuse from retirement.
    assert all(s.closed for s in first), (
        "the second file's warm inherited the first file's progress line"
    )
    trace.resume.set()
    await _drain(trace)


@pytest.mark.asyncio
async def test_a_warm_drops_a_line_that_was_taken_from_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An index starting mid-warm retires the warm's session.

    Holding on to the closed object made every later report a silent no-op, so
    the warm ran to completion with no line and no way to see it was running.
    """
    monkeypatch.setattr(tuning, "PREVIEW_WARM_DELAY", 0.0)
    trace = _CoverageTrace(["A", "B"])
    p = trace.presenter
    trace.pause_after = ("A", 4)
    p.request_full_warm("A", 0)
    await asyncio.wait_for(trace.paused.wait(), timeout=5.0)
    progress = p._app._progress
    taken = progress.ambient
    assert taken is not None, "setup: the warm should hold the line"

    progress.begin(object())  # an index takes the ambient slot
    assert taken.closed, "setup: the real facility retires the previous holder"

    trace.resume.set()
    await _drain(trace)
    assert not taken.reports_after_close, (
        "the warm kept reporting into a line that had been retired under it"
    )


@pytest.mark.asyncio
async def test_stopping_a_warm_does_not_claim_it_finished(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Closing paints the bar full and files the run for calibration.

    Three of the four retirement paths are abandonments — cancel, query reset
    and switching file — and a cancelled warm filled its bar to 100% beside a
    toast saying it had been stopped.
    """
    monkeypatch.setattr(tuning, "PREVIEW_WARM_DELAY", 0.0)
    trace = _CoverageTrace(["A", "B"])
    p = trace.presenter
    trace.pause_after = ("A", 4)
    p.request_full_warm("A", 0)
    await asyncio.wait_for(trace.paused.wait(), timeout=5.0)
    session = p._app._progress.ambient
    assert session is not None, "setup: the warm should hold a line"

    assert p.cancel_full_warm("A") is True
    assert session.abandoned, "a stopped warm was retired as though it had completed"
    trace.resume.set()
    await _drain(trace)


@pytest.mark.asyncio
async def test_a_chunk_the_builder_cannot_take_is_walked_but_not_counted() -> None:
    """The progress line's denominator counts CAPTURABLE chunks only.

    Counting every target walked mixed two units: a file whose chunks are half
    flat-path reached 100% halfway through and then sat motionless for the rest
    of the run.
    """
    trace = _CoverageTrace(["A"])
    p = trace.presenter
    del p._capture_file_targets  # the real one, not the trace's recorder

    chunks = [
        _StubChunk(0),
        _StubChunk(1, kind="pdf", body_md=""),
        _StubChunk(2),
        _StubChunk(3, kind="pdf", body_md=""),
    ]
    servable = [uses_markdown_renderer(cast("Any", c)) for c in chunks]
    assert servable == [True, False, True, False], "setup: needs a mix of both paths"

    class _Cap:
        def __init__(self, seq: int) -> None:
            self.chunk_seq = seq
            self.width = 80
            self.height = 1

    async def _capture(chunk: Any, width: int, **_kw: Any) -> _Cap:
        return _Cap(chunk.chunk_seq)

    p._warm_host = type("_H", (), {"capture": staticmethod(_capture)})()
    p.capture_store.put = lambda *_a, **_k: None  # type: ignore[attr-defined]

    seen: list[tuple[int, int]] = []
    await p._capture_file_targets(
        "A",
        "sig",
        80,
        chunks,
        list(range(len(chunks))),
        MatchSpec(),
        lambda: True,
        on_capture=lambda walked, captured: seen.append((walked, captured)),
    )
    assert seen == [(1, 1), (2, 1), (3, 2), (4, 2)], (
        f"a flat-path chunk was counted as captured progress: {seen}"
    )


@pytest.mark.asyncio
async def test_the_freeze_sweep_asks_the_markers_to_re_measure(
    tmp_path: Path, tmp_index_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sweep rewrites ``chunk_widgets`` / ``match_targets``, which is where
    the ▲▼ markers get their stops — so the counts it invalidates must be
    re-derived, not left describing the tree it just removed."""
    # The app sweeps on its own during startup, and under load it wins: this
    # sweep then has nothing to freeze, owes no notification, and the test
    # would be waiting for something that is correctly not coming. Hold the
    # automatic sweep off until the explicit one below.
    monkeypatch.setenv("_FND_NO_FREEZE", "1")
    index = wide_doc(tmp_path, tmp_index_dir)
    app = FNDApp(index_dir=index, initial_query="quartzfin")
    async with app.run_test(size=(100, 30)) as pilot:
        await wait_until(
            pilot,
            lambda: bool(app._search.groups) and app._preview.active is not None,
            timeout=30.0,
            message="preview never became active",
        )
        container = app._preview.active
        assert container is not None
        searcher = app._search.searcher
        assert searcher is not None
        chunks = searcher.get_file_chunks(container.parent_doc_id)
        mounted = sorted(container.mounted_indices)
        assert len(mounted) >= 2, "need mounted chunks for the sweep to have anything to do"

        asked = 0
        original = app._match_nav.on_preview_scrolled

        def counted() -> None:
            nonlocal asked
            asked += 1
            original()

        app._match_nav.on_preview_scrolled = counted  # type: ignore[method-assign]
        before = len(container.chunk_widgets)
        monkeypatch.delenv("_FND_NO_FREEZE")
        await app._preview._freeze_chunks_outside_window(
            container, chunks, mounted[-1] + 1, mounted[-1] + 1
        )
        # The positive control, and it must count what THIS sweep froze: chunks
        # that were already stand-ins prove nothing about the notification.
        swapped = sum(1 for w in container.chunk_widgets.values() if isinstance(w, FrozenChunkView))
        assert swapped, "the sweep froze nothing, so the notification proves nothing"
        assert before == len(container.chunk_widgets)
        # Deferred off the sweep, so let the refresh it was posted to run.
        await wait_until(
            pilot,
            lambda: asked > 0,
            timeout=15.0,
            message="the sweep swapped chunks without asking the markers to re-measure",
        )
