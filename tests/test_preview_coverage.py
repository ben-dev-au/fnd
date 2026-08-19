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
import dataclasses
import time
from pathlib import Path
from typing import cast

import pytest
from textual.app import App, ComposeResult
from textual.containers import VerticalScroll

from fnd.index import build_index
from fnd.matching import MatchSpec
from fnd.query import FileChunk
from fnd.tui import FNDApp
from fnd.tui.preview import tuning
from fnd.tui.preview.coverage import coverage_targets, filler_targets, neighbour_order
from fnd.tui.preview.frozen import FrozenChunkView, freeze
from fnd.tui.preview.warm_host import WarmHost
from fnd.tui.widgets.markdown import FNDMarkdown
from tests._pilot_wait import settle, wait_until


def _wide_doc(tmp_path: Path, tmp_index_dir: Path) -> Path:
    """A file past the full-mount budget, with hits spread far apart."""
    notes = tmp_path / "notes"
    notes.mkdir()
    lines: list[str] = ["# Wide doc", ""]
    for section in range(320):
        lines.append(f"## Section {section}")
        lines.append(
            f"quartzfin marker in section {section}."
            if section % 25 == 0
            else f"Filler prose for section {section}."
        )
        lines.extend([f"More filler line {i} for section {section}." for i in range(4)])
        lines.append("")
    (notes / "wide.md").write_text("\n".join(lines), encoding="utf-8")
    build_index(roots=[notes], index_dir=tmp_index_dir, collection="notes")
    return tmp_index_dir


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


def test_the_filler_tier_covers_what_the_hits_did_not() -> None:
    held = {4, 5, 6}
    filler = filler_targets(total=20, focus_idx=0, already=held, budget=500)
    assert sorted(filler) == [i for i in range(20) if i not in held]
    assert filler[0] == 0, "filler is nearest-first too"


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


@pytest.mark.asyncio
async def test_a_far_jump_mounts_a_capture_instead_of_building(
    tmp_path: Path, tmp_index_dir: Path
) -> None:
    index = _wide_doc(tmp_path, tmp_index_dir)
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
    index = _wide_doc(tmp_path, tmp_index_dir)
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
    index = _wide_doc(tmp_path, tmp_index_dir)
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

    def slow_freeze(chunk, chunk_seq):  # type: ignore[no-untyped-def]
        swept.append(chunk_seq)
        time.sleep(0.01)
        return real_freeze(chunk, chunk_seq)

    monkeypatch.setattr(frozen_mod, "freeze", slow_freeze)

    index = _wide_doc(tmp_path, tmp_index_dir)
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
        await app._preview._freeze_chunks_outside_window(
            container, chunks, mounted[-1] + 1, mounted[-1] + 1
        )
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
    # Each freeze is padded to 10ms, and at least 8 chunks are swept, so an
    # unsliced sweep blocks for 80ms+ in one go. Sliced, no single block should
    # exceed roughly one slice plus one chunk.
    assert worst_ms < 60, (
        "the freeze sweep held the loop through the whole swap — the "
        f"cold-to-warm transition is one uninterruptible block ({worst_ms:.0f}ms)"
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

    index = _wide_doc(tmp_path, tmp_index_dir)
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
    index = _wide_doc(tmp_path, tmp_index_dir)
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
                pane.content_size.width,
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
            pane.content_size.width,
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
    index = _wide_doc(tmp_path, tmp_index_dir)
    app = FNDApp(index_dir=index, initial_query="quartzfin")
    async with app.run_test(size=(100, 30)) as pilot:
        await wait_until(
            pilot,
            lambda: bool(app._search.groups) and app._preview.active is not None,
            timeout=20.0,
            message="preview never became active",
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
        live = next((w for w in container.chunk_widgets.values() if w.size.width > 0), None)
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
    index = _wide_doc(tmp_path, tmp_index_dir)
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
        assert capture.height > 400, (
            f"chunk only rendered {capture.height} rows, so it never overflowed the "
            f"jig's layout box and this test cannot detect the bug"
        )
        assert capture.width == requested, (
            f"asked for {requested} columns and got {capture.width}: the off-screen "
            f"container grew a scrollbar, so the capture is cut for a width it will "
            f"not be displayed at"
        )
