"""The progress line: how it draws, and when it is on screen.

The visibility rules are the whole point of this widget, so they are
tested directly against an injected clock rather than inferred from a
running app. Between them they encode the four complaints the line
exists to answer: it must appear at the start of the action, never flash,
always reach 100%, and never be retired by someone else's teardown.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from rich.style import Style as RichStyle

from fnd.tui.progress.bar import (
    FILL_GLYPH,
    TRACK_GLYPH,
    FNDProgressBar,
    progress_line_segments,
)
from fnd.tui.progress.facility import ProgressFacility, ProgressSession
from fnd.tui.progress.model import OperationPlan, Phase

# ── rendering ────────────────────────────────────────────────────


def painted(width: int, fraction: float, label: str = "") -> str:
    return "".join(
        s.text for s in progress_line_segments(width=width, fraction=fraction, label=label)
    )


# Sentinels so the two runs can be told apart in a test. In the app they are
# the resolved component styles; here any distinct pair will do.
_FILL_STYLE = RichStyle(color="red")
_TRACK_STYLE = RichStyle(color="blue")
_LABEL_STYLE = RichStyle(color="green")


def split(width: int, fraction: float, label: str = "") -> tuple[int, int]:
    """(filled cells, track cells).

    Fill and track are drawn with the SAME glyph — they differ only in
    colour — so the painted text cannot tell them apart. Classify by style
    instead, which is what actually carries the distinction on screen.
    """
    segments = progress_line_segments(
        width=width,
        fraction=fraction,
        label=label,
        fill_style=_FILL_STYLE,
        track_style=_TRACK_STYLE,
        label_style=_LABEL_STYLE,
    )
    filled = sum(len(s.text) for s in segments if s.style == _FILL_STYLE)
    # The gap before a label also wears the track style, but it is spaces.
    track = sum(len(s.text) for s in segments if s.style == _TRACK_STYLE and s.text.strip())
    return filled, track


def test_the_line_spans_the_full_width() -> None:
    """Root cause 1: the old bar was a fixed 32 cells regardless of the
    terminal, so it read as a stub in the corner."""
    for width in (40, 80, 200):
        assert len(painted(width, 0.5)) == width


def test_the_line_is_drawn_at_the_pane_border_weight() -> None:
    """It sits in the frame rather than on top of it, so it must not be
    heavier than the borders it runs under."""
    assert FILL_GLYPH == TRACK_GLYPH == "─"
    assert set(painted(20, 0.5)) == {"─"}


def test_an_empty_line_is_all_track() -> None:
    assert split(20, 0.0) == (0, 20)


def test_a_complete_line_is_all_fill() -> None:
    assert split(20, 1.0) == (20, 0)


def test_any_progress_at_all_shows_at_least_one_cell() -> None:
    """1% of a 200-cell line rounds to 2 cells, but 1% of a 40-cell line
    rounds to 0 — and a bar that reads as empty while work is happening is
    the complaint, not the fix."""
    assert split(40, 0.001)[0] >= 1


def test_a_line_short_of_done_always_leaves_a_cell_unfilled() -> None:
    """So that a full line means finished, and nothing else does."""
    assert split(20, 0.999)[1] >= 1


def test_the_label_is_right_aligned_after_the_bar() -> None:
    out = painted(40, 0.5, "page 4 of 9")
    assert out.endswith("page 4 of 9")
    assert len(out) == 40


def test_a_label_that_would_crowd_out_the_bar_is_dropped() -> None:
    assert split(20, 0.5, "a rather long label indeed") == (10, 10)
    assert len(painted(20, 0.5, "a rather long label indeed")) == 20


def test_zero_width_renders_nothing() -> None:
    assert progress_line_segments(width=0, fraction=0.5) == []


def test_fraction_is_clamped() -> None:
    assert split(10, -5.0) == (0, 10)
    assert split(10, 5.0) == (10, 0)


def test_the_widget_starts_idle() -> None:
    assert FNDProgressBar().is_idle


# ── visibility policy ────────────────────────────────────────────


class FakeClock:
    def __init__(self) -> None:
        self.now = 500.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class StubBar:
    def __init__(self) -> None:
        self.fraction = 0.0
        self.label = ""
        self.visible = False

    @property
    def is_idle(self) -> bool:
        return not self.visible

    def show(self) -> None:
        self.visible = True

    def hide(self) -> None:
        self.visible = False


class StubTimer:
    """Models Textual's Timer where it matters: ``stop()`` cancels the task
    and drops the reference, which is the liveness signal. (``_active`` is the
    PAUSE flag and ``stop`` sets it — a detail worth encoding, because reading
    it as liveness produces a check that is always True.)"""

    def __init__(self) -> None:
        self.stopped = False
        self._task: object | None = object()

    def stop(self) -> None:
        self.stopped = True
        self._task = None

    def die(self) -> None:
        """A timer killed from outside, e.g. a cancelled task."""
        self._task = None


class StubApp:
    """The slice of App the facility touches."""

    def __init__(self, bar: StubBar) -> None:
        self.bar = bar
        self.timers: list[StubTimer] = []
        self.watchdogs: list[tuple[StubTimer, Any]] = []

    def query_one(self, _selector: Any) -> StubBar:
        return self.bar

    def set_interval(self, _interval: float, _callback: Any, name: str = "") -> StubTimer:
        timer = StubTimer()
        self.timers.append(timer)
        return timer

    def set_timer(self, _delay: float, callback: Any, name: str = "") -> StubTimer:
        timer = StubTimer()
        self.watchdogs.append((timer, callback))
        return timer


ONE_PHASE = OperationPlan(
    operation_id="test.op",
    phases=(Phase(key="work", expected_ms=1000.0),),
)


def make_facility() -> tuple[ProgressFacility, StubBar, FakeClock]:
    bar = StubBar()
    clock = FakeClock()
    facility = ProgressFacility(StubApp(bar), clock=clock)  # type: ignore[arg-type]
    return facility, bar, clock


def run_until_idle(
    facility: ProgressFacility, clock: FakeClock, *, limit: int = 200
) -> list[float]:
    """Tick at the facility's own rate until the line clears; return every
    fraction that was on screen while it was visible."""
    seen: list[float] = []
    bar = facility._widget()
    assert bar is not None
    for _ in range(limit):
        if not bar.visible:  # type: ignore[attr-defined]
            break
        seen.append(bar.fraction)  # type: ignore[attr-defined]
        clock.advance(1 / 20)
        facility.tick()
    return seen


def test_the_line_paints_on_the_frame_the_session_opens() -> None:
    """No show delay: the feedback belongs to the keypress that caused it."""
    facility, bar, _clock = make_facility()
    facility.begin(ONE_PHASE)
    assert bar.visible


def test_instant_work_still_shows_a_complete_line() -> None:
    """The flash: a cache hit that finishes in 40 ms used to blink the bar
    for a frame at ~0%. It must now show, fill, and read as done."""
    facility, bar, clock = make_facility()
    session = facility.begin(ONE_PHASE)
    clock.advance(0.04)
    session.close()
    seen = run_until_idle(facility, clock)
    assert len(seen) >= 8, "line was retired inside the minimum-visible budget"
    assert max(seen) == pytest.approx(1.0)
    assert not bar.visible


def test_the_line_reaches_full_before_it_clears() -> None:
    """Never 'nothing, nothing, 20%, gone'."""
    facility, _bar, clock = make_facility()
    session = facility.begin(ONE_PHASE)
    clock.advance(0.3)
    facility.tick()
    session.close()
    seen = run_until_idle(facility, clock)
    assert seen[-1] == pytest.approx(1.0)


def test_the_visible_fraction_never_goes_backwards() -> None:
    facility, _bar, clock = make_facility()
    session = facility.begin(ONE_PHASE)
    for _ in range(10):
        clock.advance(1 / 20)
        facility.tick()
    session.close()
    seen = run_until_idle(facility, clock)
    assert seen == sorted(seen)


def test_a_stale_holder_cannot_retire_a_newer_session() -> None:
    """Root cause 4: hide_progress_bar() closed whatever was active, so any
    late teardown path could take down the bar of the navigation that had
    just replaced it."""
    facility, bar, _clock = make_facility()
    stale = facility.begin(ONE_PHASE)
    fresh = facility.begin(ONE_PHASE)
    stale.close()
    assert facility.active is fresh
    assert bar.visible


def test_a_superseded_session_hands_its_fill_to_the_next() -> None:
    """Holding Down through a result list must not saw the bar back to
    zero on every keypress."""
    facility, _bar, clock = make_facility()
    facility.begin(ONE_PHASE)
    for _ in range(12):
        clock.advance(1 / 20)
        facility.tick()
    carried = facility.displayed_fraction
    assert carried > 0.1
    facility.begin(ONE_PHASE)
    assert facility.displayed_fraction == pytest.approx(min(carried, 0.5))


def test_a_completed_session_does_not_hand_off() -> None:
    """A finished operation followed by a new one is a new activity, and
    starts from zero rather than inheriting a near-full bar."""
    facility, _bar, clock = make_facility()
    session = facility.begin(ONE_PHASE)
    clock.advance(1.0)
    facility.tick()
    session.close()
    run_until_idle(facility, clock)
    facility.begin(ONE_PHASE)
    assert facility.displayed_fraction == pytest.approx(0.0)


def test_a_sampler_ends_the_session_when_the_work_is_done() -> None:
    facility, bar, clock = make_facility()
    alive = [True]
    facility.begin(ONE_PHASE, sampler=lambda _s: alive[0])
    clock.advance(1 / 20)
    facility.tick()
    assert bar.visible
    alive[0] = False
    clock.advance(1 / 20)
    facility.tick()
    assert facility.active is None


def test_a_broken_sampler_releases_the_line_instead_of_holding_it() -> None:
    def boom(_session: ProgressSession) -> bool:
        raise RuntimeError("observer bug")

    facility, _bar, clock = make_facility()
    facility.begin(ONE_PHASE, sampler=boom)
    clock.advance(1 / 20)
    facility.tick()
    assert facility.active is None


def test_a_session_whose_owner_died_is_released() -> None:
    """A sampler that never says it is finished cannot hold the line for good:
    once the fill stops advancing, the line has nothing left to say."""
    facility, _bar, clock = make_facility()
    facility.begin(ONE_PHASE, sampler=lambda _s: True)
    for _ in range(120):
        clock.advance(1.0)
        facility.tick()
        if facility.active is None:
            break
    assert facility.active is None


def test_the_ticker_stops_once_the_line_is_idle() -> None:
    facility, _bar, clock = make_facility()
    session = facility.begin(ONE_PHASE)
    session.close()
    run_until_idle(facility, clock)
    assert facility._timer is None


# ── the legacy determinate API ───────────────────────────────────


def test_open_still_drives_a_determinate_bar() -> None:
    facility, bar, _clock = make_facility()
    session = facility.open("indexing", total=4)
    session.set_progress(3)
    assert session.total == 4
    assert session.progress == 3
    assert bar.fraction == pytest.approx(0.75)


# ── the widget inside a real app ─────────────────────────────────


@pytest.mark.asyncio
async def test_the_mounted_widget_renders_a_full_width_line(
    fixtures_dir: Path, tmp_index_dir: Path
) -> None:
    """The pure function is tested above; this covers the part it cannot —
    resolving the component styles from CSS and sizing to the real pane
    width. Asserts on the widget's own render output rather than the
    screen: a headless compositor paints blank regardless."""
    from fnd.index import build_index
    from fnd.tui import FNDApp

    build_index(roots=[fixtures_dir], index_dir=tmp_index_dir, collection="default")
    app = FNDApp(index_dir=tmp_index_dir)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        widget = app.query_one(FNDProgressBar)
        pane = app.query_one("#preview_pane")
        assert widget.is_idle, "the line must be blank until something happens"
        idle_pane_region = pane.region

        widget.show()
        widget.fraction = 0.5
        await pilot.pause()

        # Showing the line must not move the panes. Textual reports a hidden
        # widget's own region as empty, but its layout slot is reserved, so
        # the pane above keeps its geometry across the toggle.
        assert pane.region == idle_pane_region

        segments = list(widget.render().segments)  # type: ignore[attr-defined]
        text = "".join(s.text for s in segments)
        assert len(text) == widget.content_size.width > 40
        assert set(text) == {FILL_GLYPH, TRACK_GLYPH}
        # The two runs must be distinguishable, or progress reads as nothing.
        assert len({s.style for s in segments if s.text}) == 2

        widget.hide()
        await pilot.pause()
        assert pane.region == idle_pane_region


# ── the tick loop must not be a single point of failure ──────────


def test_a_tick_that_raises_does_not_escape() -> None:
    """Textual hands a timer-callback exception to App._handle_exception,
    which takes the whole app down. A progress bar must not be able to do
    that to the session it exists to serve."""
    facility, _bar, _clock = make_facility()

    def boom(_session: ProgressSession) -> bool:
        raise RuntimeError("sampler exploded in a way _sample does not catch")

    facility.begin(ONE_PHASE, sampler=boom)
    facility.tick()  # must not raise


def test_a_dead_timer_is_replaced_rather_than_trusted() -> None:
    """_start_ticking must not treat a stopped timer as "already ticking"."""
    facility, _bar, _clock = make_facility()
    app: StubApp = facility._app  # type: ignore[assignment]

    facility.begin(ONE_PHASE)
    assert len(app.timers) == 1
    app.timers[0].die()  # as Textual does when a callback raises

    facility.begin(ONE_PHASE)
    assert len(app.timers) == 2, "a dead tick loop was never replaced"
    assert facility._timer is app.timers[1]


def test_a_live_timer_is_not_churned() -> None:
    facility, _bar, _clock = make_facility()
    app: StubApp = facility._app  # type: ignore[assignment]
    facility.begin(ONE_PHASE)
    facility.begin(ONE_PHASE)
    facility.begin(ONE_PHASE)
    assert len(app.timers) == 1


def test_the_watchdog_clears_a_line_the_tick_loop_abandoned() -> None:
    """The guarantee that does not depend on the tick loop at all. Whatever
    goes wrong upstream, the line goes away."""
    facility, bar, _clock = make_facility()
    app: StubApp = facility._app  # type: ignore[assignment]

    facility.begin(ONE_PHASE, sampler=lambda _s: True)
    assert bar.visible
    app.timers[0].die()  # tick loop is gone; nothing will clear this

    assert app.watchdogs, "no watchdog was armed"
    _timer, fire = app.watchdogs[-1]
    fire()

    assert not bar.visible, "the watchdog did not put the line away"
    assert facility.active is None


def test_a_stalled_line_is_retired_even_while_a_session_claims_to_be_working() -> None:
    """A session whose sampler never finishes, and whose fraction has stopped
    moving, is not telling the user anything. It gets retired."""
    facility, bar, clock = make_facility()
    facility.begin(ONE_PHASE, sampler=lambda _s: True)
    for _ in range(400):  # 20s at the tick rate
        clock.advance(1 / 20)
        facility.tick()
        if not bar.visible:
            break
    assert not bar.visible, "a line that stopped moving stayed on screen"


def test_superseding_sessions_cannot_extend_a_stalled_line() -> None:
    """The reported failure. The paint check re-enters render_full_doc on a
    failed reveal, and each re-entry begins a new session. Measuring the cap
    from the last begin handed a stuck line a fresh budget every time, so it
    outlived any cap. The cap measures painted movement instead."""
    facility, bar, clock = make_facility()
    facility.begin(ONE_PHASE, sampler=lambda _s: True)
    for i in range(600):
        clock.advance(1 / 20)
        if i % 20 == 0:  # a re-dispatch storm, one every second
            facility.begin(ONE_PHASE, sampler=lambda _s: True)
        facility.tick()
        if not bar.visible:
            break
    assert not bar.visible, "repeated begins kept a stalled line alive"


def test_the_watchdog_is_disarmed_on_a_normal_clear() -> None:
    facility, _bar, clock = make_facility()
    session = facility.begin(ONE_PHASE)
    session.close()
    run_until_idle(facility, clock)
    assert facility._watchdog is None


def test_an_active_session_never_paints_a_full_line() -> None:
    """A full line means finished. A session whose phases have all eased out
    is not finished, so it must still show a gap — otherwise a stall there
    looks identical to a stall in the clear."""
    facility, bar, clock = make_facility()
    facility.begin(ONE_PHASE, sampler=lambda _s: True)
    for _ in range(200):
        clock.advance(0.05)
        facility.tick()
        if facility.active is None:
            break
        assert bar.fraction < 1.0


def test_an_idle_facility_releases_its_tick_loop() -> None:
    facility, _bar, _clock = make_facility()
    facility.begin(ONE_PHASE)
    facility._active = None  # neither active nor completing
    facility._completing_at = None
    facility.tick()
    assert facility._timer is None


def test_a_changing_label_counts_as_movement() -> None:
    """Indexing one large PDF holds the file counter still for minutes; the
    page counter in the label is the only thing showing it is alive. Retiring
    that line as stalled would be exactly backwards."""
    facility, bar, clock = make_facility()
    session = facility.begin(ONE_PHASE, sampler=lambda _s: True)
    for page in range(1, 60):
        clock.advance(1.0)
        session.set_label(f"Module_06.pdf · page {page} of 118")
        facility.tick()
        assert bar.visible, f"a line with a live page counter was retired at page {page}"
    assert session is facility.active
