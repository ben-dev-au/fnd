"""Operation plans, and the trackers that drive them.

Each subsystem the line serves contributes two things and nothing else:

* an :class:`~fnd.tui.progress.model.OperationPlan` — what its phases are,
  what each is expected to cost, and whether the user is waiting on it
  (:class:`~fnd.tui.progress.model.OperationKind`);
* a tracker satisfying :class:`~fnd.tui.progress.facility.ProgressTracker`,
  which reads that subsystem's own pipeline and translates whatever it
  counts into ``enter``/``report``.

That translation at the boundary is what lets one line serve operations
with no unit in common. Rendered lines, mounted chunks and indexed files
never meet; each tracker turns its own units into ``report(done, total)``
and the phase weights turn the rest into one 0..1 fraction. Adding a
subsystem means adding a plan and a tracker — the facility, the widget
and the calibration store need no knowledge of it.

Seed durations come from the measured navigation budget;
:mod:`fnd.tui.progress.calibration` replaces them with this machine's own
medians after a few runs, keyed on ``operation_id``.

The preview tracker **reads** pipeline state rather than being called
from inside the pipeline. Two reasons, both load-bearing:

* The old design put a ``show``/``hide`` pair at every exit of the mount
  path — sixteen call sites, each guarded by hand — and any stale one
  could retire a newer navigation's bar. Deriving "is anything in
  flight?" from the pipeline's own signals cannot go stale.
* It keeps this work out of the mount/scroll code that is being changed
  in parallel for latency.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from fnd.tui.progress.model import OperationKind, OperationPlan, Phase

if TYPE_CHECKING:
    from fnd.tui.app import FNDApp
    from fnd.tui.progress.facility import ProgressSession

# ── Preview navigation ───────────────────────────────────────────
#
# Cold: a file the pane is not already showing. Seeds are the medians of
# the per-phase budget measured on a 1018-chunk PDF — decode off-thread,
# the ±window mount, the focus chunk's build (the single biggest item),
# and the reconcile→scroll commit.
PREVIEW_COLD = OperationPlan(
    operation_id="preview.cold",
    phases=(
        Phase(key="decode", expected_ms=300.0),
        Phase(key="mount", expected_ms=250.0, countable=True),
        Phase(key="build", expected_ms=700.0),
        Phase(key="land", expected_ms=550.0),
    ),
)

# The FLAT path (PDF, TXT) installs a prebuilt document into one shared
# LineBufferPreview: dispatch_flat_mount never assigns a mount task and leaves
# ``active`` as None, so ``mount`` and ``build`` are not slow on that path —
# they are unreachable. Giving a flat navigation the structural plan handed
# 53% of the bar to phases it could never enter, so the fill was capped there
# and the line stopped partway every time. A plan must only contain phases the
# path it describes can actually reach.
# One phase, because one is all this path exposes. dispatch_flat_mount arms
# the scroll anchor and reconciles SYNCHRONOUSLY inside the decode callback,
# so is_settling is never observed and a `land` phase here was entered zero
# times in 29 navigations while holding 36% of the bar. Everything after the
# decode happens in a single unobservable step; pretending otherwise is what
# left the fill at a median of 0.167.
#
# Seeded BELOW the measured median, deliberately. This phase's duration spans
# more than an order of magnitude (p25 226 ms, median ~1080 ms, p75 3135 ms),
# so no single expectation fits and the choice is which way to be wrong. The
# curve is asymmetric: a phase that overruns creeps asymptotically from 0.78
# towards 0.97, which still reads as progress, while one that finishes early
# is capped proportionally — 55% of the expected duration means a bar that
# stops at 0.43. For a high-variance phase, under-estimating is the graceful
# failure. Two real-corpus runs measured medians of 1325 ms and 1767 ms.
PREVIEW_COLD_FLAT = OperationPlan(
    operation_id="preview.cold.flat",
    phases=(Phase(key="decode", expected_ms=1000.0, countable=True),),
)

PREVIEW_WARM_FLAT = OperationPlan(
    operation_id="preview.warm.flat",
    phases=(Phase(key="decode", expected_ms=250.0, countable=True),),
)

# Warm: no decode to do, because the chunks are already cached. Same phases
# so the observer needs no special case, but its own operation id — mixing a
# genuinely cold monster into these medians would make both estimates useless.
#
# These are measured, not guessed, and the first guess was badly wrong: warm
# was seeded at 10/40/40/90 on the assumption that a cache hit is nearly free.
# On a real corpus a warm navigation costs ~1030 ms — the cache saves the
# decode, not the mount, the focus build or the scroll commit. Two effects,
# and the second is the one that showed on screen: every phase ran 8-15x its
# expectation and spent its whole life in the eased overrun region, and the
# implied WEIGHTS were wrong besides (land held half the bar for a phase that
# is really a seventh of the work). Fill at completion scattered between 0.35
# and 0.97 as a result. Calibration corrects this within a few navigations,
# but only for someone who already has a history; the seeds are what every
# fresh install runs on.
PREVIEW_WARM = OperationPlan(
    operation_id="preview.warm",
    phases=(
        Phase(key="decode", expected_ms=100.0),
        Phase(key="mount", expected_ms=280.0, countable=True),
        Phase(key="build", expected_ms=60.0),
        Phase(key="land", expected_ms=40.0),
    ),
)

# A query is INTERACTIVE — the user is waiting on it — even though it is the
# one operation whose work happens entirely off the loop.
SEARCH = OperationPlan(
    operation_id="search",
    phases=(
        Phase(key="query", expected_ms=400.0),
        Phase(key="results", expected_ms=120.0),
    ),
)

# Indexing is the one AMBIENT operation: nobody asked for it just now, it
# outlasts any navigation by orders of magnitude, and it must therefore give
# the line up to whatever the user does next and take it back afterwards.
INDEX = OperationPlan(
    operation_id="index",
    phases=(
        Phase(key="scan", expected_ms=4000.0),
        Phase(key="files", expected_ms=60_000.0, countable=True),
        Phase(key="commit", expected_ms=3000.0),
    ),
    kind=OperationKind.AMBIENT,
)


# Warming one file whole, on request. AMBIENT for the same reason indexing is:
# it outlasts any navigation by orders of magnitude, so it has to give the line
# up to whatever the user does next and take it back afterwards. Countable
# because the unit is a chunk and there are hundreds — a fraction alone would
# read as a stalled bar. Measured on a real corpus: 69s for an 854-chunk book,
# 167s for a 719-chunk PDF, so the seed is an order of magnitude, not a promise.
WARM_WHOLE_FILE = OperationPlan(
    operation_id="preview.warm_whole",
    phases=(Phase(key="capture", expected_ms=100_000.0, countable=True),),
    kind=OperationKind.AMBIENT,
)


class PreviewProgressTracker:
    """Samples the preview pipeline once per progress tick.

    Phase order is the pipeline's own order, and the model only ever moves
    forwards, so a navigation that skips a stage (cached chunks, no decode)
    simply retires it.
    """

    def __init__(self, app: FNDApp, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._app = app
        self._clock = clock
        # When the pipeline was last doing work, so the land phase can be
        # bounded without depending on a flag that may never clear.
        self._busy_at: float = 0.0
        # The file the navigation in flight asked for, so arrival can be
        # recognised rather than inferred from the pipeline going quiet.
        self._target: str | None = None

    # ── lifecycle ────────────────────────────────────────────────

    def plan_for(self, parent_id: str) -> OperationPlan:
        """Pick the plan for this navigation.

        Two axes, and both matter:

        * **Warm vs cold** — asks ``showing_parent()`` rather than reading
          ``active`` directly, because the flat path leaves ``active`` as None
          and reading it classified every flat navigation as cold, including a
          jump inside an already-open PDF.
        * **Flat vs structural** — a plan must only contain phases its path can
          reach (see PREVIEW_COLD_FLAT). Decided through
          ``uses_markdown_renderer``, the same predicate the dispatcher routes
          on, so the two cannot drift.
        """
        # "Warm" means the work ahead is cheap. Three things can make it so,
        # in descending strength:
        #
        # * the pane is already showing this file, so there is nothing to open;
        # * coverage has captured every listed hit, so each mount is a blit
        #   rather than a markdown build (see fnd/tui/preview/coverage.py) —
        #   this is now the strongest predictor, and the chunk cache alone
        #   cannot see it;
        # * the chunks are decoded, so at least the decode is skipped.
        #
        # File size, tried first, is a far weaker signal — a 54x size range
        # produced only a 3.6x duration range, because the preview mounts a
        # fixed window however large the file is.
        preview = self._app._preview
        state = preview.file_warm_state(parent_id)
        warm = (
            preview.showing_parent() == parent_id
            or (state is not None and state.is_served)
            or parent_id in preview.chunk_cache
        )
        if self._is_structural(parent_id):
            return PREVIEW_WARM if warm else PREVIEW_COLD
        return PREVIEW_WARM_FLAT if warm else PREVIEW_COLD_FLAT

    def _is_structural(self, parent_id: str) -> bool:
        """Whether this file will take the structural renderer.

        Answered from the search hits, which satisfy the same protocol as the
        chunks the dispatcher inspects — so this works at ``begin`` time, before
        anything has been decoded. Unknown files are treated as structural: that
        plan is a superset, so a wrong guess costs pacing rather than making a
        phase unreachable.
        """
        import os

        from fnd.tui.preview_dispatcher import uses_markdown_renderer

        # Mirror the dispatcher's own escape hatch, or the plan would describe
        # the structural path while the pane took the flat one.
        if os.environ.get("_FND_FORCE_FLAT") == "1":
            return False

        for group in self._app._search.groups:
            if group.parent_id != parent_id:
                continue
            hits = getattr(group, "hits", ()) or ()
            if not hits:
                return True
            return any(uses_markdown_renderer(h) for h in hits)
        return True

    def begin(self, parent_id: str) -> ProgressSession:
        self._busy_at = self._clock()
        self._target = parent_id
        return self._app._progress.begin(self.plan_for(parent_id), sampler=self.sample)

    def _arrived(self, preview: Any, scroll: Any) -> bool:
        """Whether the match this navigation asked for is on screen and still.

        Three conditions, and the third is not optional: ``is_painted``
        excludes a container behind ``-pre-reveal``, ``showing_parent``
        confirms it is the right file, and the scroll must have committed.
        Without the last one, arrival short-circuited the settle and the line
        cleared while the view was still moving — measured at 3 navigations in
        30, which is the "it vanished before it finished" failure rather than
        the lingering one.

        A scroll that never settles cannot strand this: arrival simply stays
        false and the fallback below bounds it — ``_landing`` gives up after
        the app's own reveal watchdog, and ``pipeline_busy`` goes quiet.
        """
        if self._target is None:
            return False
        try:
            return (
                preview.showing_parent() == self._target
                and preview.is_painted()
                and not scroll.is_settling
            )
        except Exception:
            return False

    # ── sampling ─────────────────────────────────────────────────

    def sample(self, session: ProgressSession) -> bool:
        """Advance ``session`` to whatever the pipeline is doing now.
        Returns False once the navigation has landed."""
        preview = self._app._preview
        scroll = self._app._preview_scroll
        if preview.pipeline_busy():
            self._busy_at = self._clock()

        if self._decoding(preview):
            self._advance(session, "decode")
            self._report_decode(session, preview)
        elif self._mounting(preview):
            if self._advance(session, "mount"):
                self._report_mount(session, preview)
        elif self._building(preview):
            self._advance(session, "build")
        elif self._landing(preview, scroll):
            self._advance(session, "land")

        # Done when the MATCH IS ON SCREEN — not when the pipeline runs dry.
        #
        # Those were the same thing until the capture cache landed. Now the
        # mount keeps filling below the fold long after the visible window has
        # arrived, and waiting for it left the line up over a second after the
        # user could read the match: measured on a real corpus, TRAIL p90 735
        # ms and max 1493 ms, against 202 ms before the merge. Every one of
        # those samples was held by ``pipeline_busy`` alone.
        #
        # A full line has to mean "your match is there". Work continuing
        # underneath is real, but it is not work the user is waiting on, and
        # reporting it is how the line earns the "it lingers" complaint.
        #
        # ``pipeline_busy`` stays in as the fallback for the case ``_arrived``
        # cannot answer: before the target is known, or when the pane is
        # showing nothing at all.
        busy = not self._arrived(preview, scroll) and (
            bool(preview.pipeline_busy()) or self._landing(preview, scroll)
        )
        if not busy:
            # The work is over — so every phase of it is over, including the
            # ones no sample ever landed inside.
            #
            # This is not bookkeeping. An observer polling at 20 Hz only sees
            # a phase if a tick falls inside it, and the event loop is
            # saturated during exactly the navigation this line reports on
            # (measured: it blocks for 400-1274 ms at a stretch), so ticks are
            # dropped and the pipeline can pass through build and land between
            # two samples. Measured on a real corpus: 7 of 31 navigations
            # finished while the tracker still believed they were mounting,
            # and mount is followed by 53% of the bar — so the line sat at a
            # median of 0.50 and then jumped to full. That jump is the
            # "pauses halfway, then completes" report.
            self._advance(session, session.plan.phases[-1].key)
        return busy

    def _landing(self, preview: Any, scroll: Any) -> bool:
        """A navigation whose scroll has not committed yet.

        This is the last phase and it carries real time — the measured
        reconcile-to-scroll-commit window is 440-740ms — so getting it wrong
        is expensive in both directions.

        Gating it on ``inflight_target`` (an earlier attempt) made it
        UNREACHABLE: ``reveal_active`` clears that latch, so by the time an
        uncommitted scroll is the only outstanding work it is already gone.
        Measured on a real corpus: the phase was entered zero times in sixteen
        navigations while still holding 31% of the bar's weight, so every cold
        navigation was capped near 20-40% and read as "it pauses halfway".

        Gating on ``is_settling`` alone is the opposite failure: it is set when
        a navigation arms its anchor and cleared only when THAT scroll
        commits, and ``dispatch_mount`` has paths that rebuild without ever
        reconciling, so it can stay set for good.

        So: still landing while the scroll is outstanding AND there is
        something on screen to land on (every reset path clears that) AND the
        pipeline went idle only recently. The grace is the app's own bound on
        how long a reveal may take — past it, the navigation is over whatever
        the scroll controller believes.
        """
        if not scroll.is_settling:
            return False
        if preview.showing_parent() is None:
            return False
        from fnd.tui.preview import tuning

        return (self._clock() - self._busy_at) * 1000.0 < tuning.REVEAL_WATCHDOG_MS

    @staticmethod
    def _advance(session: ProgressSession, phase: str) -> bool:
        """Enter ``phase`` if this session's plan has one.

        The flat and structural plans deliberately carry different phases, and
        the pipeline signals are not exclusive — a structural mount left over
        from the previous navigation can still be in flight while a flat
        session is active. Entering a phase the plan does not contain would
        raise, and the sampler's catch-all would then retire the line rather
        than the navigation ending it.
        """
        if not any(p.key == phase for p in session.plan.phases):
            return False
        session.enter(phase)
        return True

    # ── pipeline signals ─────────────────────────────────────────

    @staticmethod
    def _decoding(preview: Any) -> bool:
        worker = preview.decode_worker
        if worker is None:
            return False
        try:
            return not worker.is_finished
        except Exception:
            return False

    @staticmethod
    def _mounting(preview: Any) -> bool:
        task = preview.mount_task
        if task is None:
            return False
        try:
            return not task.done()
        except Exception:
            return False

    @staticmethod
    def _building(preview: Any) -> bool:
        container = preview.active
        task = getattr(container, "_finalise_task", None) if container is not None else None
        if task is None:
            return False
        try:
            return not task.done()
        except Exception:
            return False

    @staticmethod
    def _report_decode(session: ProgressSession, preview: Any) -> None:
        """Real line counts from the flat renderer, when it is reporting.

        The flat path's duration spans more than an order of magnitude
        (p25 226ms, p75 3135ms on a real corpus) and nothing observable at
        dispatch predicts it — file size scales it by roughly its fourth root,
        because only a window is ever mounted. Estimating it was therefore
        never going to be accurate, so the renderer counts its own lines and
        this reads them. Falls back to the timed estimate when the count is
        absent, which is the structural path and the pre-render moments of the
        flat one.
        """
        from fnd.tui.preview import decode_progress

        done, total = decode_progress.snapshot(getattr(preview, "decode_token", 0))
        if total > 0:
            session.report(min(done, total), total)

    def _report_mount(self, session: ProgressSession, preview: Any) -> None:
        """Fraction of the *window* mounted — not of the file.

        The old bar divided by ``len(chunks)``, so a 1018-chunk PDF whose
        mount only ever lands a ±7 window topped out near 1%. The window is
        what this phase actually builds, so it is the honest denominator.
        """
        container = preview.active
        if container is None:
            return
        from fnd.tui.preview import tuning

        # The window the mount ACTUALLY chose, when it has chosen one. It is
        # selected by rows with the chunk count only as a cap, so on a
        # tall-chunk format it can hold two chunks where the tunables suggest
        # fifteen — and pricing it at fifteen means this phase never reads as
        # finished. Falls back to the tunables before the mount has picked.
        window = getattr(container, "mount_window", 0) or (
            tuning.VISIBLE_FIRST_ABOVE + tuning.VISIBLE_FIRST_BELOW + 1
        )
        total_chunks = getattr(container, "total_chunks", 0) or 0
        denominator = min(total_chunks, window) if total_chunks else window
        mounted = len(getattr(container, "mounted_indices", ()) or ())
        session.report(min(mounted, denominator), denominator)


class IndexProgressTracker:
    """Samples a running index so a background reindex has a visible cost.

    An ``open_modal=False`` run — which is what auto-resume starts on launch —
    used to surface nothing but a toast, so the machine could be indexing for
    minutes with no indication. The IndexerScreen keeps its own richer display
    and stays the drill-in; this only mirrors the headline onto the line.

    Reads ``IndexerService.state`` rather than draining the event queue: that
    queue has a single consumer (the modal), and a second reader would steal
    its events.

    Its plan is AMBIENT, which is what lets a run that spans hundreds of
    navigations survive them. It is also the only tracker that sets a label:
    with no label the line says "something is happening", which is all a
    navigation needs, but a background run the user did not start has to say
    what it is.
    """

    def __init__(self, app: FNDApp) -> None:
        self._app = app

    def begin(self) -> ProgressSession:
        return self._app._progress.begin(INDEX, sampler=self.sample)

    def sample(self, session: ProgressSession) -> bool:
        service = self._app._indexer
        task = service.task
        if task is None:
            return False
        try:
            if task.done():
                return False
        except Exception:
            return False

        state = service.state
        total = (getattr(state, "total_files", 0) or 0) if state is not None else 0
        if total <= 0:
            # Still walking the sources; there is no denominator yet.
            session.enter("scan")
            return True
        session.enter("files")
        session.report(getattr(state, "files_completed", 0) or 0, total)
        session.set_label(self._label(service, state, total))
        return True

    def _label(self, service: Any, state: Any, total: int) -> str:
        """Value only: what is being indexed and how far in.

        A slow PDF gets the page counter too — during a multi-minute
        texturising the file counter alone does not move, which is the exact
        shape of "it looks frozen".
        """
        done = getattr(state, "files_completed", 0) or 0
        collection = getattr(service, "collection", "") or ""
        parts: list[str] = []
        if collection:
            chain_total = getattr(service, "chain_total", 1) or 1
            if chain_total > 1:
                # Shared with IndexerScreen's title so the two cannot drift —
                # and so this keeps the clamp, which it was missing: a state
                # where chain_remaining still holds every collection rendered
                # as "CPL (0 of 4)".
                from fnd.tui.indexer_service import chain_position

                parts.append(f"{collection} ({chain_position(service)} of {chain_total})")
            else:
                parts.append(collection)
        parts.append(f"{done} of {total} files")

        from fnd.tui import live_progress

        path, pages_done, pages_total, _started = live_progress.snapshot()
        if pages_total > 0 and path:
            from pathlib import Path

            parts.append(f"{Path(path).name} · page {pages_done} of {pages_total}")
        return " · ".join(parts)


__all__ = [
    "INDEX",
    "PREVIEW_COLD",
    "PREVIEW_COLD_FLAT",
    "PREVIEW_WARM",
    "PREVIEW_WARM_FLAT",
    "SEARCH",
    "WARM_WHOLE_FILE",
    "IndexProgressTracker",
    "PreviewProgressTracker",
]
