"""The progress line: how it draws, and when it is on screen.

The visibility rules are the whole point of this widget, so they are
tested directly against an injected clock rather than inferred from a
running app. Between them they encode the four complaints the line
exists to answer: it must appear at the start of the action, never flash,
always reach 100%, and never be retired by someone else's teardown.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from rich.style import Style as RichStyle

from fnd.tui.progress.bar import (
    FILL_GLYPH,
    TRACK_GLYPH,
    FNDProgressBar,
    progress_line_segments,
)
from fnd.tui.progress.facility import ProgressFacility, ProgressSession
from fnd.tui.progress.model import OperationKind, OperationPlan, Phase
from tests._progress_stubs import FakeClock, StubBar, StubProgressApp

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


ONE_PHASE = OperationPlan(
    operation_id="test.op",
    phases=(Phase(key="work", expected_ms=1000.0),),
)


def make_facility() -> tuple[ProgressFacility, StubBar, FakeClock]:
    bar = StubBar()
    clock = FakeClock()
    facility = ProgressFacility(StubProgressApp(bar), clock=clock)  # type: ignore[arg-type]
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

        # render() is a Rich Text for the bar row (and a Group once the status
        # row exists), so read the spans rather than assuming segments.
        rendered = widget.render()
        text = rendered.plain  # type: ignore[attr-defined]
        assert len(text) == widget.content_size.width > 40
        assert set(text) == {FILL_GLYPH, TRACK_GLYPH}
        # The two runs must be distinguishable, or progress reads as nothing.
        styles = {span.style for span in rendered.spans}  # type: ignore[attr-defined]
        assert len(styles) == 2

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
    app: StubProgressApp = facility._app  # type: ignore[assignment]

    facility.begin(ONE_PHASE)
    assert len(app.timers) == 1
    app.timers[0].die()  # as Textual does when a callback raises

    facility.begin(ONE_PHASE)
    assert len(app.timers) == 2, "a dead tick loop was never replaced"
    assert facility._timer is app.timers[1]


def test_a_live_timer_is_not_churned() -> None:
    facility, _bar, _clock = make_facility()
    app: StubProgressApp = facility._app  # type: ignore[assignment]
    facility.begin(ONE_PHASE)
    facility.begin(ONE_PHASE)
    facility.begin(ONE_PHASE)
    assert len(app.timers) == 1


def test_the_watchdog_clears_a_line_the_tick_loop_abandoned() -> None:
    """The guarantee that does not depend on the tick loop at all. Whatever
    goes wrong upstream, the line goes away."""
    facility, bar, _clock = make_facility()
    app: StubProgressApp = facility._app  # type: ignore[assignment]

    facility.begin(ONE_PHASE, sampler=lambda _s: True)
    assert bar.visible
    app.timers[0].die()  # tick loop is gone; nothing will clear this

    assert app.watchdogs, "no watchdog was armed"
    _timer, fire = app.watchdogs[-1]
    fire()

    # The backstop COMPLETES rather than just hiding: a line that vanishes
    # part-filled is the symptom it exists to prevent, so it paints full first
    # and schedules the clear independently of the tick loop it cannot trust.
    assert facility.active is None
    assert bar.visible
    assert bar.fraction == pytest.approx(1.0)

    final_clear = app.watchdogs[-1][1]
    final_clear()
    assert not bar.visible, "the watchdog never put the line away"


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


SLOW_PHASE = OperationPlan(
    operation_id="test.slow",
    phases=(Phase(key="work", expected_ms=60_000.0),),
)


def test_an_unchanged_frame_costs_nothing() -> None:
    """``fraction`` is a Textual reactive, so assigning it repaints the row.
    On a slow phase the eased value changes by a fraction of a percent per
    tick — the same cells — so painting unconditionally spent 20 repaints a
    second drawing the identical thing, event-loop work during exactly the
    navigation this line exists to smooth. Measured on a real 3 s session:
    61 repaints before this guard, 11 after."""
    facility, bar, clock = make_facility()
    facility.begin(SLOW_PHASE, sampler=lambda _s: True)
    for _ in range(60):  # 3 s at the tick rate
        clock.advance(1 / 20)
        facility.tick()
    # Over 3 s of a 60 s phase the fill crosses only a handful of cells, so
    # the vast majority of those 60 ticks must cost nothing.
    assert bar.paints <= 8, f"redundant repaints: {bar.paints} across 60 ticks"


def test_every_visible_change_is_painted_exactly_once() -> None:
    """The guard must skip only frames that would draw the same thing."""
    facility, bar, clock = make_facility()
    facility.begin(ONE_PHASE, sampler=lambda _s: True)
    seen: set[int] = set()
    for _ in range(60):
        clock.advance(1 / 20)
        facility.tick()
        seen.add(round(bar.fraction * bar.content_size.width))
    # +1 for the paint begin() itself does, before the first tick.
    assert bar.paints <= len(seen) + 1, f"{bar.paints} repaints for {len(seen)} distinct frames"


def test_a_changed_cell_still_repaints() -> None:
    """The guard must not suppress real movement."""
    facility, bar, _clock = make_facility()
    session = facility.begin(ONE_PHASE)
    before = bar.paints
    session.report(1, 4)
    session.report(2, 4)
    session.report(3, 4)
    assert bar.paints >= before + 3


def test_the_stall_cap_retires_before_the_watchdog() -> None:
    """The tick loop is the normal retirement path; the watchdog only covers a
    tick loop that is gone. With the watchdog set below the stall cap it beat
    the loop to every retirement, so the stall branch was dead code and every
    case went through _force_clear — a superseded retirement, which skips both
    the completion animation and the calibration sample."""
    from fnd.tui.progress import facility as facility_mod

    assert facility_mod._WATCHDOG_MARGIN_S > 0

    facility, bar, clock = make_facility()
    session = facility.begin(SLOW_PHASE, sampler=lambda _s: True)
    for _ in range(int(facility_mod._STALL_CAP_S * 20) + 4):
        clock.advance(1 / 20)
        facility.tick()
        if facility.active is None:
            break
    assert session.closed
    # Retired by the tick loop, so the completion animation still runs.
    assert facility._completing_at is not None
    assert bar.visible


def test_the_watchdog_survives_a_widget_it_cannot_resolve() -> None:
    """query_one fails whenever a modal screen is on top — which is exactly
    what a background index sits behind. Dropping the backstop there would
    retire it for the rest of the session, since only real progress re-arms
    it and a stalled session makes none."""
    facility, _bar, _clock = make_facility()
    app: StubProgressApp = facility._app  # type: ignore[assignment]
    facility.begin(ONE_PHASE, sampler=lambda _s: True)

    def no_widget(_selector: object) -> object:
        raise RuntimeError("NoMatches — a modal is on top")

    app.query_one = no_widget  # type: ignore[method-assign]
    before = len(app.watchdogs)
    facility._force_clear()
    assert len(app.watchdogs) == before + 1, "backstop dropped on a transient failure"


# ── arbitration: one line, two classes of work ───────────────────

AMBIENT_PLAN = OperationPlan(
    operation_id="test.ambient",
    phases=(Phase(key="work", expected_ms=60_000.0),),
    kind=OperationKind.AMBIENT,
)


def settle(facility: ProgressFacility, clock: FakeClock, *, ticks: int = 40) -> None:
    """Tick past a completion hold so whatever comes next can take the line."""
    for _ in range(ticks):
        clock.advance(1 / 20)
        facility.tick()


def test_a_navigation_takes_the_line_from_a_background_run() -> None:
    facility, bar, _clock = make_facility()
    facility.begin(AMBIENT_PLAN, label="CPL · 3 of 40 files", sampler=lambda _s: True)
    assert bar.ambient is True

    facility.begin(ONE_PHASE, sampler=lambda _s: True)
    assert bar.ambient is False, (
        "a background run painted over the operation the user is waiting on"
    )


def test_a_background_run_never_paints_over_a_navigation() -> None:
    """Order reversed: the index starting mid-navigation must not grab the
    line either, or a reindex triggered by a scope change would blank the
    feedback for the navigation it triggered."""
    facility, bar, _clock = make_facility()
    facility.begin(ONE_PHASE, sampler=lambda _s: True)
    facility.begin(AMBIENT_PLAN, label="CPL", sampler=lambda _s: True)
    assert bar.ambient is False
    assert bar.label == ""


def test_a_background_run_gets_the_line_back_after_a_navigation() -> None:
    """The defect this whole two-slot arrangement exists to fix.

    With one slot, ``begin`` was last-writer-wins: every navigation retired
    the running index permanently. A reindex spans hundreds of navigations,
    so in practice the line showed it until the user's first keypress and
    then never again — and at launch the initial query beat it to that.
    """
    facility, bar, clock = make_facility()
    index = facility.begin(AMBIENT_PLAN, label="CPL · 3 of 40 files", sampler=lambda _s: True)

    nav = facility.begin(ONE_PHASE, sampler=lambda _s: True)
    nav.close()
    settle(facility, clock)

    assert not index.closed, "the background run was retired by an unrelated navigation"
    assert bar.visible
    assert bar.ambient is True
    # Its text is on the status row, never beside the bar.
    assert bar.label == ""
    assert bar.status == "CPL · 3 of 40 files"


def test_a_background_run_that_ended_unseen_does_not_come_back() -> None:
    """Sampled before it repaints, so a run that finished while the line was
    busy elsewhere neither flashes a stale bar nor announces itself late —
    the same rule as one that ends while still suspended, which is what
    keeps the two indistinguishable to the user."""
    facility, bar, clock = make_facility()
    alive = True
    facility.begin(AMBIENT_PLAN, label="CPL", sampler=lambda _s: alive)

    nav = facility.begin(ONE_PHASE, sampler=lambda _s: True)
    alive = False
    nav.close()
    settle(facility, clock)

    assert not bar.visible
    assert bar.ambient is False
    assert facility._completing_at is None, "a run that ended unseen announced itself late"


def test_a_background_run_finishing_unseen_does_not_steal_the_completion() -> None:
    """Its "done" belongs to the indexer's toast. Taking the line back to
    flash a full bar would interrupt the navigation the user is watching."""
    facility, bar, _clock = make_facility()
    index = facility.begin(AMBIENT_PLAN, label="CPL", sampler=lambda _s: True)
    facility.begin(ONE_PHASE, sampler=lambda _s: True)

    index.close()
    assert facility._completing_at is None
    assert bar.ambient is False


def test_a_background_run_outlasts_a_burst_of_navigation() -> None:
    """The stall cap counts silence, and a suspended session is silent by
    construction — so resuming has to forgive the gap it did not cause."""
    facility, bar, clock = make_facility()
    index = facility.begin(AMBIENT_PLAN, label="CPL", sampler=lambda _s: True)
    for _ in range(30):
        nav = facility.begin(ONE_PHASE, sampler=lambda _s: True)
        clock.advance(0.5)
        nav.close()
        settle(facility, clock)
    assert not index.closed
    assert bar.visible
    assert bar.ambient is True


def test_the_backstops_scale_with_the_class_of_work() -> None:
    """A source walk over an evicted cloud vault genuinely reports nothing
    for minutes. Retiring it on the navigation budget would put the line
    away in the middle of the longest wait in the app."""
    from fnd.tui.progress import facility as facility_mod

    assert facility_mod._AMBIENT_STALL_CAP_S > facility_mod._STALL_CAP_S * 10

    for plan, cap in (
        (ONE_PHASE, facility_mod._STALL_CAP_S),
        (AMBIENT_PLAN, facility_mod._AMBIENT_STALL_CAP_S),
    ):
        facility, _bar, _clock = make_facility()
        app: StubProgressApp = facility._app  # type: ignore[assignment]
        facility.begin(plan, sampler=lambda _s: True)
        assert app.timer_delays, f"{plan.operation_id}: no watchdog was armed"
        assert app.timer_delays[-1] > cap, (
            f"{plan.operation_id}: the watchdog beats its own stall cap, "
            "so the stall path is dead code"
        )


def test_both_classes_of_work_are_visible_at_once() -> None:
    """The bar answers the keypress; the status row says what is running on
    its own. Before the second row they took turns, so with two things going
    the user could only ever see one of them — and could not tell which."""
    facility, bar, _clock = make_facility()
    facility.begin(AMBIENT_PLAN, label="CPL · 3 of 40 files", sampler=lambda _s: True)
    facility.begin(ONE_PHASE, sampler=lambda _s: True)

    assert bar.ambient is False, "the bar should be showing the navigation"
    assert bar.label == ""
    assert bar.status == "CPL · 3 of 40 files", (
        "the background run vanished the moment the user navigated"
    )


def test_the_status_row_keeps_up_while_it_is_suspended() -> None:
    """A file counter that froze the instant you touched an arrow key would
    be worse than no counter: it reads as a stalled index."""
    facility, bar, _clock = make_facility()
    index = facility.begin(AMBIENT_PLAN, label="CPL · 3 of 40 files", sampler=lambda _s: True)
    facility.begin(ONE_PHASE, sampler=lambda _s: True)

    index.set_label("CPL · 11 of 40 files")
    assert bar.status == "CPL · 11 of 40 files"


def test_the_status_row_goes_away_with_the_run_that_owns_it() -> None:
    """It costs a row of preview height while it exists, so it may not
    outlive the thing it reports on."""
    facility, bar, clock = make_facility()
    alive = True
    facility.begin(AMBIENT_PLAN, label="CPL", sampler=lambda _s: alive)
    assert bar.status == "CPL"

    alive = False
    settle(facility, clock)
    assert bar.status == ""


def test_background_text_never_lands_in_the_bars_row() -> None:
    """`─` is drawn at the middle of its cell and text sits on a baseline near
    the bottom of one, so a label sharing the bar's row reads as crowding the
    footer with a gap above it. That is the font, not the layout — the fix is
    to keep the background run's text out of that row entirely, which is what
    the status row is for.

    Checked with the ambient run holding the line on its own, since that is
    the case that used to put its label beside the bar.
    """
    facility, bar, _clock = make_facility()
    facility.begin(AMBIENT_PLAN, label="CPL · 3 of 40 files", sampler=lambda _s: True)
    assert bar.ambient is True, "setup — the background run should hold the line"
    assert bar.label == "", "background text is back in the bar's row"
    assert bar.status == "CPL · 3 of 40 files"
