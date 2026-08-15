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
        self.inflight_target: tuple[str, int] | None = None
        self.busy = False

    def pipeline_busy(self) -> bool:
        return self.busy


class StubScroll:
    def __init__(self) -> None:
        self.is_settling = False


class StubBar:
    def __init__(self) -> None:
        self.fraction = 0.0
        self.label = ""
        self.visible = False

    def show(self) -> None:
        self.visible = True

    def hide(self) -> None:
        self.visible = False


class StubApp:
    def __init__(self) -> None:
        self._preview = StubPreview()
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


@pytest.fixture
def tracker(app: StubApp) -> PreviewProgressTracker:
    return PreviewProgressTracker(app)  # type: ignore[arg-type]


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
    app._preview.inflight_target = ("doc", 0)
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
    app._preview.inflight_target = ("doc", 0)
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
    app._preview.inflight_target = ("doc", 0)
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


def test_a_finished_preview_releases_the_line_even_if_the_scroll_never_commits(
    app: StubApp, tracker: PreviewProgressTracker
) -> None:
    """The reported stall. dispatch_mount has paths that cancel and rebuild
    without reconciling, so is_settling can stay true for good — and with the
    pane fully loaded, active and parent_id stay set too. The in-flight latch
    is the signal that actually ends: every completion path clears it, and the
    reveal watchdog clears it even when no reveal happens.
    """
    session = tracker.begin("doc")
    app._preview.busy = False
    app._preview_scroll.is_settling = True  # never commits
    app._preview.active = StubContainer(parent_doc_id="doc", total_chunks=40, mounted=40)
    app._preview.parent_id = "doc"
    app._preview.inflight_target = None  # ...but the navigation finished

    assert tracker.sample(session) is False, (
        "a loaded preview held the line open on a scroll flag that never clears"
    )
