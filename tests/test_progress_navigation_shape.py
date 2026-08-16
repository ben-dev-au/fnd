"""The shape of the line across a whole navigation.

This is the complaint stated as an assertion. Driving a scripted
pipeline through the real tracker and the real facility, frame by frame
at the facility's own rate, the line must:

* be up on the first frame of the navigation,
* stay up continuously until the match has landed,
* only ever move forwards,
* never sit still long enough to read as a stall, and
* reach 100% before it clears.

The old bar failed four of those five.
"""

from __future__ import annotations

from itertools import pairwise
from typing import Any

import pytest

from fnd.tui.progress.facility import ProgressFacility
from fnd.tui.progress.operations import PreviewProgressTracker
from tests._progress_stubs import StubBar

TICK = 1 / 20
# The pass condition from the design: no interval longer than this where the
# line is visible and not moving.
MAX_DEAD_S = 0.25
MAX_DEAD_FRAMES = int(MAX_DEAD_S / TICK)


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class ScriptedPipeline:
    """A preview pipeline on a timetable.

    Phases run back to back for the durations given; after the last one
    everything reads idle, which is how the tracker learns the navigation
    landed.
    """

    def __init__(self, clock: FakeClock, schedule: list[tuple[str, float]]) -> None:
        self._clock = clock
        self._start = clock()
        self._schedule = schedule
        self.active = _Container()
        self.parent_id = "doc"
        # The in-flight latch the real presenter sets in fire_pending_load and
        # clears when the navigation lands.
        self.inflight_target: tuple[str, int] | None = ("doc", 0)

    # -- the signals the tracker samples -----------------------------

    @property
    def _stage(self) -> str | None:
        elapsed = self._clock() - self._start
        for name, seconds in self._schedule:
            if elapsed < seconds:
                return name
            elapsed -= seconds
        return None

    @property
    def decode_worker(self) -> Any:
        return _Worker() if self._stage == "decode" else None

    @property
    def mount_task(self) -> Any:
        if self._stage != "mount":
            return None
        self.active.advance_mount()
        return _Task()

    def pipeline_busy(self) -> bool:
        return self._stage in {"decode", "mount", "build"}

    def sync_latch(self) -> None:
        """Clear the in-flight latch once the schedule is done, the way every
        completion path in the presenter does."""
        self.inflight_target = ("doc", 0) if self._stage is not None else None

    def sync_finalize(self) -> None:
        """The real presenter spawns a detached finalize task and hangs it on
        the container; the tracker reads that to know it is in the build
        phase. Model it, or the build stage is invisible and the tracker
        retires it early."""
        self.active._finalize_task = _Task() if self._stage == "build" else None


class _Worker:
    is_finished = False


class _Task:
    @staticmethod
    def done() -> bool:
        return False


class _Container:
    def __init__(self, parent_doc_id: str = "previous-file") -> None:
        # The pane is showing a DIFFERENT file when the navigation starts —
        # that is what makes this a cold navigation. Leaving it as "doc" had
        # the tracker pick the warm plan, whose seeds total 180 ms, and then
        # every phase of this 2.3 s schedule overran by an order of magnitude.
        self.parent_doc_id = parent_doc_id
        self.total_chunks = 1018
        self.mounted_indices: set[int] = set()
        self._finalize_task: Any = None

    def advance_mount(self) -> None:
        self.mounted_indices.add(len(self.mounted_indices))


class StubScroll:
    def __init__(self, pipeline: ScriptedPipeline) -> None:
        self._pipeline = pipeline

    @property
    def is_settling(self) -> bool:
        return self._pipeline._stage in {"decode", "mount", "build", "land"}


class StubApp:
    def __init__(self, bar: StubBar, clock: FakeClock, schedule: list[tuple[str, float]]) -> None:
        self.bar = bar
        self._preview = ScriptedPipeline(clock, schedule)
        self._preview_scroll = StubScroll(self._preview)
        self._progress = ProgressFacility(self, clock=clock)  # type: ignore[arg-type]

    def query_one(self, _selector: Any) -> StubBar:
        return self.bar

    def set_interval(self, _interval: float, _cb: Any, name: str = "") -> Any:
        return _Timer()


class _Timer:
    def stop(self) -> None:
        return None


# The measured cold budget: decode, the +/- window mount, the focus chunk's
# build, and the reconcile -> scroll commit. ~2.3 s in total, which is the
# slow end of the real cold-navigation range.
COLD_SCHEDULE = [("decode", 0.30), ("mount", 0.25), ("build", 1.20), ("land", 0.55)]


def run_navigation(schedule: list[tuple[str, float]]) -> list[tuple[bool, float]]:
    """Drive one navigation and return (visible, fraction) per frame."""
    bar = StubBar()
    clock = FakeClock()
    app = StubApp(bar, clock, schedule)
    tracker = PreviewProgressTracker(app)  # type: ignore[arg-type]
    tracker.begin("doc")

    frames = [(bar.visible, bar.fraction)]
    for _ in range(400):
        clock.advance(TICK)
        app._preview.sync_finalize()
        app._preview.sync_latch()
        app._progress.tick()
        frames.append((bar.visible, bar.fraction))
        if not bar.visible and len(frames) > 2:
            break
    return frames


# A plateau only reads as a stall if the user can SEE it stop. Measure in
# painted cells on a typical-width line, not in floats: successive fractions
# differing in the fourth decimal are identical on screen, and comparing them
# with == let a 1.4-second freeze pass as movement.
LINE_CELLS = 100


def longest_still_run(fractions: list[float]) -> int:
    """Longest run of frames that paint the same number of filled cells."""
    cells = [int(f * LINE_CELLS) for f in fractions]
    longest = current = 0
    for a, b in pairwise(cells):
        current = current + 1 if b == a else 0
        longest = max(longest, current)
    return longest


def test_the_line_is_up_from_the_first_frame() -> None:
    frames = run_navigation(COLD_SCHEDULE)
    assert frames[0][0] is True


def test_the_line_stays_up_for_the_whole_navigation() -> None:
    """No blink, no gap between phases. The old bar was opened and closed
    by whichever stage happened to run, so it flickered between them."""
    frames = run_navigation(COLD_SCHEDULE)
    visible = [v for v, _ in frames]
    first_hidden = visible.index(False) if False in visible else len(visible)
    assert all(visible[:first_hidden])
    assert first_hidden * TICK > sum(seconds for _, seconds in COLD_SCHEDULE)


def test_the_fraction_only_moves_forwards() -> None:
    frames = run_navigation(COLD_SCHEDULE)
    fractions = [f for v, f in frames if v]
    assert fractions == sorted(fractions)


def test_the_line_never_stalls_long_enough_to_read_as_frozen() -> None:
    """The "nothing, nothing, nothing" test. The build phase alone is 1.2 s
    with nothing to count; the line must still be moving through it."""
    frames = run_navigation(COLD_SCHEDULE)
    moving = [f for v, f in frames if v and f < 1.0]
    assert longest_still_run(moving) <= MAX_DEAD_FRAMES


def test_the_line_reaches_full_before_it_clears() -> None:
    frames = run_navigation(COLD_SCHEDULE)
    visible = [f for v, f in frames if v]
    assert visible[-1] == pytest.approx(1.0)


def test_the_line_gets_meaningfully_far_through_the_slow_phases() -> None:
    """Guards the seeds: if the weights were wrong the bar would spend the
    whole navigation in the first tenth, which is what it used to do."""
    frames = run_navigation(COLD_SCHEDULE)
    midpoint = frames[len(frames) // 2][1]
    assert 0.25 < midpoint < 0.95


def test_an_instant_navigation_still_shows_a_complete_line() -> None:
    frames = run_navigation([("mount", 0.02)])
    visible = [f for v, f in frames if v]
    assert len(visible) >= 8, "retired inside the minimum-visible budget"
    assert visible[-1] == pytest.approx(1.0)


# A file far slower than anything recorded before: the build phase runs five
# times its seed. This is the worst case for a timed phase — the first time
# this machine ever meets such a file, nothing has been calibrated.
OVERRUN_SCHEDULE = [("decode", 0.30), ("mount", 0.25), ("build", 3.50), ("land", 0.55)]
# What the curve can honestly guarantee on that first, uncalibrated encounter.
# Measured: the exponential ease this replaced froze for 1.30 s here.
MAX_DEAD_UNCALIBRATED_S = 0.60


def test_a_badly_overrunning_phase_still_creeps() -> None:
    """A phase running 5x its expectation must degrade, not freeze. The
    asymptote means it cannot fill; the 1/t tail means it keeps moving."""
    frames = run_navigation(OVERRUN_SCHEDULE)
    moving = [f for v, f in frames if v and f < 1.0]
    worst = longest_still_run(moving) * TICK
    assert worst <= MAX_DEAD_UNCALIBRATED_S


def test_the_second_visit_is_paced_by_the_first() -> None:
    """Calibration is what turns the timed phases from a guess into a pace.
    Meeting the same slow file again must be visibly smoother."""
    first = longest_still_run([f for v, f in run_navigation(OVERRUN_SCHEDULE) if v and f < 1.0])
    second = longest_still_run([f for v, f in run_navigation(OVERRUN_SCHEDULE) if v and f < 1.0])
    assert second < first
    assert second * TICK <= MAX_DEAD_S
