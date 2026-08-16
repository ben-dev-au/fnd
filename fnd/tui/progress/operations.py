"""Operation plans, and the observers that drive them.

Seed durations come from the measured navigation budget (see
``dev/audits/PREVIEW_LATENCY_INVESTIGATION.md`` and the real-terminal
timings behind it); :mod:`fnd.tui.progress.calibration` replaces them
with this machine's own medians after a few runs.

The preview observer **reads** pipeline state rather than being called
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

from fnd.tui.progress.model import OperationPlan, Phase

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
PREVIEW_COLD_FLAT = OperationPlan(
    operation_id="preview.cold.flat",
    phases=(Phase(key="decode", expected_ms=350.0),),
)

PREVIEW_WARM_FLAT = OperationPlan(
    operation_id="preview.warm.flat",
    phases=(Phase(key="decode", expected_ms=80.0),),
)

# Warm: a match jump inside the file already on screen. Same phases so the
# observer needs no special case, but its own operation id — mixing 50 ms
# jumps into the cold medians would make both estimates useless.
PREVIEW_WARM = OperationPlan(
    operation_id="preview.warm",
    phases=(
        Phase(key="decode", expected_ms=10.0),
        Phase(key="mount", expected_ms=40.0, countable=True),
        Phase(key="build", expected_ms=40.0),
        Phase(key="land", expected_ms=90.0),
    ),
)

SEARCH = OperationPlan(
    operation_id="search",
    phases=(
        Phase(key="query", expected_ms=400.0),
        Phase(key="results", expected_ms=120.0),
    ),
)

INDEX = OperationPlan(
    operation_id="index",
    phases=(
        Phase(key="scan", expected_ms=4000.0),
        Phase(key="files", expected_ms=60_000.0, countable=True),
        Phase(key="commit", expected_ms=3000.0),
    ),
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
        warm = self._app._preview.showing_parent() == parent_id
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
        return self._app._progress.begin(self.plan_for(parent_id), sampler=self.sample)

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
        elif self._mounting(preview):
            if self._advance(session, "mount"):
                self._report_mount(session, preview)
        elif self._building(preview):
            self._advance(session, "build")
        elif self._landing(preview, scroll):
            self._advance(session, "land")

        return bool(preview.pipeline_busy()) or self._landing(preview, scroll)

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
        task = getattr(container, "_finalize_task", None) if container is not None else None
        if task is None:
            return False
        try:
            return not task.done()
        except Exception:
            return False

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

        total_chunks = getattr(container, "total_chunks", 0) or 0
        window = tuning.VISIBLE_FIRST_ABOVE + tuning.VISIBLE_FIRST_BELOW + 1
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
    "IndexProgressTracker",
    "PreviewProgressTracker",
]
