"""The preview observer.

The tracker reads the pipeline rather than being called from inside it,
so these tests drive it against stand-ins for the signals it samples.
The important one is ``test_mount_is_measured_against_the_window`` —
dividing by the whole file's chunk count is exactly why the old bar
topped out around 1% on a large PDF.
"""

from __future__ import annotations

from typing import Any

import pytest

from fnd.tui.preview import tuning
from fnd.tui.progress.facility import ProgressFacility
from fnd.tui.progress.operations import (
    PREVIEW_COLD,
    PREVIEW_WARM,
    PreviewProgressTracker,
)
from tests._progress_stubs import StubBar, StubSearch


class StubWorker:
    def __init__(self, finished: bool = False) -> None:
        self.is_finished = finished


class StubTask:
    def __init__(self, done: bool = False) -> None:
        self._done = done

    def done(self) -> bool:
        return self._done


class StubContainer:
    def __init__(
        self,
        parent_doc_id: str = "doc",
        total_chunks: int = 0,
        mounted: int = 0,
        finalize: StubTask | None = None,
    ) -> None:
        self.parent_doc_id = parent_doc_id
        self.total_chunks = total_chunks
        self.mounted_indices = set(range(mounted))
        self._finalize_task = finalize


class StubPreview:
    def __init__(self) -> None:
        self.decode_worker: StubWorker | None = None
        self.mount_task: StubTask | None = None
        self.active: StubContainer | None = None
        self.parent_id: str | None = None
        # The tracker reads this to tell a decode from a cache hit.
        self.chunk_cache: dict[str, object] = {}
        self.inflight_target: tuple[str, int] | None = None
        # The flat path (PDF/TXT) shows a file without setting ``active``.
        self.flat_parent: str | None = None
        self.busy = False

    def pipeline_busy(self) -> bool:
        return self.busy

    def showing_parent(self) -> str | None:
        """Mirrors PreviewPresenter.showing_parent: the flat buffer wins, then
        the active container. ``flat_parent`` stands in for the flat path,
        which leaves ``active`` as None."""
        if self.flat_parent is not None:
            return self.flat_parent
        return self.active.parent_doc_id if self.active is not None else None


class StubScroll:
    def __init__(self) -> None:
        self.is_settling = False


class StubApp:
    def __init__(self) -> None:
        self._preview = StubPreview()
        self._search = StubSearch()
        self._preview_scroll = StubScroll()
        self.bar = StubBar()
        self._progress = ProgressFacility(self)  # type: ignore[arg-type]

    def query_one(self, _selector: Any) -> StubBar:
        return self.bar

    def set_interval(self, _interval: float, _callback: Any, name: str = "") -> Any:
        return None


@pytest.fixture
def app() -> StubApp:
    return StubApp()


class TrackerClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> TrackerClock:
    return TrackerClock()


@pytest.fixture
def tracker(app: StubApp, clock: TrackerClock) -> PreviewProgressTracker:
    return PreviewProgressTracker(app, clock=clock)  # type: ignore[arg-type]


# ── which plan ───────────────────────────────────────────────────


def test_a_new_file_is_a_cold_navigation(app: StubApp, tracker: PreviewProgressTracker) -> None:
    app._preview.active = StubContainer(parent_doc_id="other")
    assert tracker.plan_for("doc") is PREVIEW_COLD


def test_a_jump_inside_the_open_file_is_warm(app: StubApp, tracker: PreviewProgressTracker) -> None:
    app._preview.active = StubContainer(parent_doc_id="doc")
    assert tracker.plan_for("doc") is PREVIEW_WARM


def test_an_empty_pane_is_a_cold_navigation(tracker: PreviewProgressTracker) -> None:
    assert tracker.plan_for("doc") is PREVIEW_COLD


# ── phase inference ──────────────────────────────────────────────


def test_the_decode_worker_puts_us_in_decode(app: StubApp, tracker: PreviewProgressTracker) -> None:
    session = tracker.begin("doc")
    app._preview.decode_worker = StubWorker(finished=False)
    app._preview.busy = True
    assert tracker.sample(session) is True
    assert session.phase == "decode"


def test_the_mount_task_puts_us_in_mount(app: StubApp, tracker: PreviewProgressTracker) -> None:
    session = tracker.begin("doc")
    app._preview.mount_task = StubTask(done=False)
    app._preview.active = StubContainer(total_chunks=40, mounted=0)
    app._preview.busy = True
    tracker.sample(session)
    assert session.phase == "mount"


def test_the_finalize_task_puts_us_in_build(app: StubApp, tracker: PreviewProgressTracker) -> None:
    session = tracker.begin("doc")
    app._preview.active = StubContainer(finalize=StubTask(done=False))
    app._preview.busy = True
    tracker.sample(session)
    assert session.phase == "build"


def test_an_uncommitted_scroll_puts_us_in_land(
    app: StubApp, tracker: PreviewProgressTracker
) -> None:
    session = tracker.begin("doc")
    app._preview.active = StubContainer(parent_doc_id="doc")
    app._preview_scroll.is_settling = True
    tracker.sample(session)
    assert session.phase == "land"


def test_a_navigation_that_skips_the_decode_still_advances(
    app: StubApp, tracker: PreviewProgressTracker
) -> None:
    """Cached chunks mean no decode at all — the phase must be retired,
    not waited on."""
    session = tracker.begin("doc")
    app._preview.mount_task = StubTask(done=False)
    app._preview.active = StubContainer(total_chunks=40)
    app._preview.busy = True
    tracker.sample(session)
    assert session.phase == "mount"
    assert session.fraction > 0.0


def test_phases_never_run_backwards(app: StubApp, tracker: PreviewProgressTracker) -> None:
    """A late decode-worker reference must not drag a landing navigation
    back to the first phase."""
    session = tracker.begin("doc")
    app._preview.active = StubContainer(parent_doc_id="doc")
    app._preview_scroll.is_settling = True
    tracker.sample(session)
    landed = session.fraction
    app._preview_scroll.is_settling = False
    app._preview.decode_worker = StubWorker(finished=False)
    app._preview.busy = True
    tracker.sample(session)
    assert session.phase == "land"
    assert session.fraction >= landed


# ── completion ───────────────────────────────────────────────────


def test_the_session_ends_when_the_pipeline_is_idle_and_the_scroll_committed(
    app: StubApp, tracker: PreviewProgressTracker
) -> None:
    session = tracker.begin("doc")
    app._preview.busy = False
    app._preview_scroll.is_settling = False
    assert tracker.sample(session) is False


def test_a_committed_scroll_is_not_enough_while_the_pipeline_works(
    app: StubApp, tracker: PreviewProgressTracker
) -> None:
    session = tracker.begin("doc")
    app._preview.busy = True
    app._preview_scroll.is_settling = False
    assert tracker.sample(session) is True


def test_an_idle_pipeline_is_not_enough_while_the_scroll_is_pending(
    app: StubApp, tracker: PreviewProgressTracker
) -> None:
    session = tracker.begin("doc")
    app._preview.busy = False
    app._preview.active = StubContainer(parent_doc_id="doc")
    app._preview_scroll.is_settling = True
    assert tracker.sample(session) is True


# ── the denominator ──────────────────────────────────────────────


def test_mount_is_measured_against_the_window(
    app: StubApp, tracker: PreviewProgressTracker
) -> None:
    """Root cause 2. A 1018-chunk PDF only ever mounts the +/- window, so
    dividing by the file left the bar reading ~1% and then vanishing."""
    window = tuning.VISIBLE_FIRST_ABOVE + tuning.VISIBLE_FIRST_BELOW + 1
    session = tracker.begin("doc")
    app._preview.mount_task = StubTask(done=False)
    app._preview.active = StubContainer(total_chunks=1018, mounted=window)
    app._preview.busy = True
    tracker.sample(session)

    mount_weight = PREVIEW_COLD.weights()[PREVIEW_COLD.index_of("mount")]
    decode_weight = PREVIEW_COLD.weights()[PREVIEW_COLD.index_of("decode")]
    assert session.fraction == pytest.approx(decode_weight + mount_weight, abs=1e-6)


def test_a_short_file_uses_its_own_chunk_count(
    app: StubApp, tracker: PreviewProgressTracker
) -> None:
    session = tracker.begin("doc")
    app._preview.mount_task = StubTask(done=False)
    app._preview.active = StubContainer(total_chunks=4, mounted=2)
    app._preview.busy = True
    tracker.sample(session)

    weights = PREVIEW_COLD.weights()
    expected = weights[0] + weights[1] * 0.5
    assert session.fraction == pytest.approx(expected, abs=1e-6)


def test_mounting_past_the_window_does_not_overflow_the_phase(
    app: StubApp, tracker: PreviewProgressTracker
) -> None:
    """The background fill keeps mounting after the window is done; the
    phase must cap rather than bleed into the next one's share."""
    session = tracker.begin("doc")
    app._preview.mount_task = StubTask(done=False)
    app._preview.active = StubContainer(total_chunks=1018, mounted=400)
    app._preview.busy = True
    tracker.sample(session)

    weights = PREVIEW_COLD.weights()
    assert session.fraction == pytest.approx(weights[0] + weights[1], abs=1e-6)


def test_a_reset_releases_the_line_even_though_the_scroll_never_committed(
    app: StubApp, tracker: PreviewProgressTracker
) -> None:
    """Nothing releases the scroll anchor on a reset, so a query that returns
    no results leaves is_settling true forever. Without the extra check the
    line would stay up until the hard cap."""
    session = tracker.begin("doc")
    app._preview_scroll.is_settling = True
    app._preview.busy = False
    app._preview.active = None
    app._preview.parent_id = None
    app._preview.inflight_target = None
    assert tracker.sample(session) is False


def test_a_scroll_that_never_commits_is_bounded_by_the_reveal_budget(
    app: StubApp, tracker: PreviewProgressTracker, clock: TrackerClock
) -> None:
    """dispatch_mount has paths that cancel and rebuild without reconciling, so
    is_settling can stay true for good. The land phase is therefore bounded by
    how long the app itself allows a reveal to take — past that the navigation
    is over whatever the scroll controller believes.

    Gating this on inflight_target instead (an earlier attempt) made the land
    phase UNREACHABLE, because reveal_active clears that latch before the
    scroll commits — which capped every cold navigation near 20-40%.
    """
    from fnd.tui.preview import tuning

    session = tracker.begin("doc")
    app._preview.busy = False
    app._preview_scroll.is_settling = True  # never commits
    app._preview.active = StubContainer(parent_doc_id="doc", total_chunks=40, mounted=40)

    assert tracker.sample(session) is True, "the land phase must be reachable"
    clock.advance(tuning.REVEAL_WATCHDOG_MS / 1000.0 + 0.1)
    assert tracker.sample(session) is False, (
        "a scroll that never commits held the line past the reveal budget"
    )


def test_the_land_phase_is_reachable_at_all(app: StubApp, tracker: PreviewProgressTracker) -> None:
    """Guards the defect directly: a phase that is never entered still keeps
    its share of the bar, so the fill silently caps below full. 'land' carries
    31% of PREVIEW_COLD, and gating it on a latch that clears too early made
    every cold navigation stop around a third of the way across."""
    session = tracker.begin("doc")
    app._preview.busy = False
    app._preview.active = StubContainer(parent_doc_id="doc")
    app._preview_scroll.is_settling = True
    tracker.sample(session)
    assert session.phase == "land"


# ── the flat path (PDF, TXT) ─────────────────────────────────────


def test_a_jump_inside_an_open_pdf_is_warm(app: StubApp, tracker: PreviewProgressTracker) -> None:
    """The flat path installs into one shared buffer and leaves ``active`` as
    None, so reading ``active`` classified every PDF navigation as cold —
    including a jump inside the file already on screen. PDFs are the heavy
    case, so that mispriced the bar and fed warm samples into the cold
    calibration."""
    app._preview.active = None
    app._preview.flat_parent = "doc"
    assert tracker.plan_for("doc") is PREVIEW_WARM


def test_a_new_pdf_is_still_cold(app: StubApp, tracker: PreviewProgressTracker) -> None:
    app._preview.active = None
    app._preview.flat_parent = "other"
    assert tracker.plan_for("doc") is PREVIEW_COLD


def test_a_signal_from_the_other_path_does_not_retire_the_line(
    app: StubApp, tracker: PreviewProgressTracker
) -> None:
    """Flat and structural plans carry different phases, and the pipeline
    signals are not exclusive: a structural mount left over from the previous
    navigation can still be in flight while a flat session is active. Entering
    a phase the plan does not have would raise, and the sampler's catch-all
    would retire the line instead of the navigation ending it."""
    from fnd.tui.progress.operations import PREVIEW_COLD_FLAT

    session = app._progress.begin(PREVIEW_COLD_FLAT, sampler=tracker.sample)
    app._preview.mount_task = StubTask(done=False)  # structural signal
    app._preview.active = StubContainer(total_chunks=40, mounted=3)
    app._preview.busy = True

    assert tracker.sample(session) is True, "a foreign signal retired the line"
    assert session.phase == "decode", "the session left its own plan"
