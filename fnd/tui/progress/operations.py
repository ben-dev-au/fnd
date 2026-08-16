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

    def __init__(self, app: FNDApp) -> None:
        self._app = app

    # ── lifecycle ────────────────────────────────────────────────

    def plan_for(self, parent_id: str) -> OperationPlan:
        """Cold unless the pane is already showing this file.

        Asks ``showing_parent()`` rather than reading ``active`` directly:
        the flat path (PDF, TXT) installs into one shared LineBufferPreview
        and leaves ``active`` as None, so reading it classified EVERY flat
        navigation as cold — including a jump inside an already-open PDF.
        That is the heavy case, and it both mispriced the bar and fed warm
        samples into the cold calibration.
        """
        return PREVIEW_WARM if self._app._preview.showing_parent() == parent_id else PREVIEW_COLD

    def begin(self, parent_id: str) -> ProgressSession:
        return self._app._progress.begin(self.plan_for(parent_id), sampler=self.sample)

    # ── sampling ─────────────────────────────────────────────────

    def sample(self, session: ProgressSession) -> bool:
        """Advance ``session`` to whatever the pipeline is doing now.
        Returns False once the navigation has landed."""
        preview = self._app._preview
        scroll = self._app._preview_scroll

        if self._decoding(preview):
            session.enter("decode")
        elif self._mounting(preview):
            session.enter("mount")
            self._report_mount(session, preview)
        elif self._building(preview):
            session.enter("build")
        elif self._landing(preview, scroll):
            session.enter("land")

        return bool(preview.pipeline_busy()) or self._landing(preview, scroll)

    @staticmethod
    def _landing(preview: Any, scroll: Any) -> bool:
        """A navigation whose scroll has not committed yet.

        ``is_settling`` cannot carry this on its own. It is set when a
        navigation arms its anchor and cleared only when THAT scroll commits;
        ``PreviewScrollController.release`` has exactly one caller in the whole
        codebase (the lazy mounter), and ``dispatch_mount`` has paths that
        cancel and rebuild without ever reconciling. Left to it, a preview that
        had finished loading could hold the line open until the hard cap —
        observed as "the bar stalled part-filled until I navigated away and
        came back".

        ``inflight_target`` closes that hole. It is set in exactly one place
        (``fire_pending_load``) and cleared in five, including
        ``reveal_active`` — which the reveal watchdog invokes within
        ``REVEAL_WATCHDOG_MS`` even when a reveal never happens, so the latch
        cannot stay set indefinitely the way the settling flag can. Requiring
        BOTH means the line lives only while the two agree work is
        outstanding, and it still ends on a reset: a committed search clears
        the latch explicitly.
        """
        return bool(scroll.is_settling) and preview.inflight_target is not None

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
    "PREVIEW_WARM",
    "SEARCH",
    "IndexProgressTracker",
    "PreviewProgressTracker",
]
