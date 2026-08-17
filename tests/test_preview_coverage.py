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
        pane_width = app.query_one("#preview_pane").content_size.width

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
        width = app.query_one("#preview_pane").content_size.width
        sig = app._search.query_signature()
        spec = app._effective_match_spec

        # A lazy-mount batch that never finishes, so the only thing that can end
        # the wait is the check under test.
        blocker: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        app._lazy.task = asyncio.ensure_future(blocker)
        try:
            targets = [i for i, c in enumerate(chunks) if c.chunk_seq == group.hits[0].chunk_seq]
            assert targets, "need a hit chunk to try to capture"
            before = presenter.capture_store.count(group.parent_id, sig, width)
            job = asyncio.create_task(
                presenter._capture_targets(group.parent_id, sig, width, chunks, targets, spec)
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
        await job
