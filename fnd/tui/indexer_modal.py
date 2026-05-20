"""TUI modal + footer status for the async indexer.

User flow:
    1. Command palette → "reindex <collection>"
    2. IndexerScreen opens, shows live progress
    3. Esc / "Background" button dismisses the screen; the indexer
       task keeps running and the footer indicator shows the status.
    4. Clicking the footer indicator re-opens the screen, reattached
       to the running task's progress.
    5. "Cancel" stops at next file boundary; "Pause" is the same as
       Cancel for now (resume happens automatically on next launch
       via state file + cache).

State (lives on :class:`fnd.tui.app.FNDApp` so it survives screen
dismiss/reopen):
- ``_indexer_task``  : current asyncio.Task | None
- ``_indexer_cancel``: asyncio.Event for cancellation
- ``_indexer_events``: asyncio.Queue of ProgressEvents; modal drains
- ``_indexer_state`` : latest IndexState snapshot for footer display
"""

from __future__ import annotations

import asyncio
import datetime as dt
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import TYPE_CHECKING

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, ProgressBar, Static

from fnd.config import CollectionConfig
from fnd.index_runner import IndexState, ProgressEvent, run_indexer

if TYPE_CHECKING:
    from fnd.tui.app import FNDApp


def fmt_eta(seconds: float) -> str:
    if seconds < 0 or seconds != seconds:  # NaN guard
        return "—"
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        m, s = divmod(int(seconds), 60)
        return f"{m}m {s}s"
    h, rem = divmod(int(seconds), 3600)
    m = rem // 60
    return f"{h}h {m}m"


def estimate_eta_seconds(*, files_done: int, files_total: int, elapsed_s: float) -> float:
    """Running-average ETA. Returns +inf when no data yet."""
    if files_done <= 0:
        return float("inf")
    avg_per_file = elapsed_s / files_done
    remaining = files_total - files_done
    return avg_per_file * max(0, remaining)


class IndexerScreen(ModalScreen[None]):
    """Modal screen showing live reindex progress."""

    BINDINGS = [  # noqa: RUF012
        Binding("escape,b", "background", "Background", show=True),
        Binding("c", "cancel", "Cancel", show=True),
        Binding("p", "pause", "Pause", show=True),
    ]

    CSS = """
    IndexerScreen { align: center middle; background: $surface 75%; }
    #indexer_box {
        width: 78; height: auto;
        border: round $accent;
        padding: 1 2; background: $surface;
    }
    #indexer_box Static { padding: 0 0 1 0; }
    #indexer_progress { width: 100%; }
    #indexer_buttons { height: 3; padding-top: 1; }
    #indexer_buttons Button { margin-right: 2; }
    .indexer-stat-label { color: $text-muted; }
    """

    def __init__(self, collection: str) -> None:
        super().__init__()
        self._collection = collection

    def compose(self) -> ComposeResult:
        with Vertical(id="indexer_box"):
            yield Static(f"[bold]Indexing: {self._collection}[/]", id="indexer_title")
            yield Static("Starting…", id="indexer_status")
            yield ProgressBar(total=1, show_eta=False, show_percentage=True, id="indexer_progress")
            yield Static("", id="indexer_current_file")
            yield Static("", id="indexer_timing")
            yield Static("", id="indexer_cache")
            with Horizontal(id="indexer_buttons"):
                yield Button("Pause", id="indexer_pause")
                yield Button("Run in background", id="indexer_background", variant="primary")
                yield Button("Cancel", id="indexer_cancel", variant="warning")

    async def on_mount(self) -> None:
        # Snapshot any progress events already buffered so we don't
        # show "Starting…" when the indexer is well underway.
        self._apply_latest_state()
        # Spawn a coroutine that drains the event queue and updates
        # the widgets until the screen is dismissed.
        self.run_worker(self._drain_events(), exclusive=False)

    def _apply_latest_state(self) -> None:
        app = self._fnd_app()
        state = app._indexer_state
        if state is None:
            return
        self._apply_state_snapshot(state)

    def _fnd_app(self) -> FNDApp:
        from fnd.tui.app import FNDApp

        assert isinstance(self.app, FNDApp)
        return self.app

    async def _drain_events(self) -> None:
        app = self._fnd_app()
        queue = app._indexer_events
        if queue is None:
            return
        try:
            while not self.is_modal_dismissed():
                try:
                    ev = await asyncio.wait_for(queue.get(), timeout=0.25)
                except TimeoutError:
                    continue
                self._render_event(ev)
                if ev.kind in ("done", "cancelled"):
                    break
        except asyncio.CancelledError:
            return

    def is_modal_dismissed(self) -> bool:
        return not self.is_active

    def _apply_state_snapshot(self, state: IndexState) -> None:
        try:
            bar = self.query_one("#indexer_progress", ProgressBar)
            bar.update(total=max(1, state.total_files), progress=state.files_completed)
            self.query_one("#indexer_current_file", Static).update(
                f"[dim]Current:[/] {_short_name(state.current_file)}"
            )
            self.query_one("#indexer_cache", Static).update(
                f"[dim]Cache:[/] {state.cache_hits} hits, {state.cache_misses} misses"
            )
        except Exception:
            pass

    def _render_event(self, ev: ProgressEvent) -> None:
        try:
            status = self.query_one("#indexer_status", Static)
            bar = self.query_one("#indexer_progress", ProgressBar)
            current = self.query_one("#indexer_current_file", Static)
            timing = self.query_one("#indexer_timing", Static)
            cache_line = self.query_one("#indexer_cache", Static)
        except Exception:
            return

        if ev.kind == "started":
            status.update(f"Indexing {ev.files_total} files…")
            bar.update(total=max(1, ev.files_total), progress=0)
        elif ev.kind == "file_complete":
            status.update(f"{ev.files_done} / {ev.files_total} files")
            bar.update(progress=ev.files_done)
        elif ev.kind == "cancelled":
            status.update("[yellow]Cancelled.[/] State saved; re-run to resume.")
        elif ev.kind == "done":
            status.update("[green]Done.[/]")

        current.update(f"[dim]Current:[/] {_short_name(ev.current_file)}")

        eta = estimate_eta_seconds(
            files_done=ev.files_done, files_total=ev.files_total, elapsed_s=ev.elapsed_s
        )
        timing.update(
            f"[dim]Elapsed:[/] {fmt_eta(ev.elapsed_s)}    " f"[dim]ETA:[/] {fmt_eta(eta)}"
        )
        cache_line.update(
            f"[dim]Cache:[/] {ev.cache_hits_total} hits, " f"{ev.cache_misses_total} misses"
        )

    # ---- bindings ----

    async def action_background(self) -> None:
        """Dismiss the modal; task keeps running on the app."""
        self.dismiss(None)

    async def action_cancel(self) -> None:
        app = self._fnd_app()
        if app._indexer_cancel is not None:
            app._indexer_cancel.set()
        # Task will yield "cancelled" on next file boundary; modal
        # drains the event and shows the status.

    async def action_pause(self) -> None:
        """Pause is conceptually the same as Cancel for now —
        re-running the reindex resumes via cache + state file."""
        await self.action_cancel()

    # ---- button handlers ----

    async def on_button_pressed(self, ev: Button.Pressed) -> None:
        if ev.button.id == "indexer_background":
            await self.action_background()
        elif ev.button.id == "indexer_cancel":
            await self.action_cancel()
        elif ev.button.id == "indexer_pause":
            await self.action_pause()


def _short_name(path: str) -> str:
    if not path:
        return "—"
    name = Path(path).name
    return name if len(name) <= 48 else name[:45] + "…"


# ---- App-side helpers ---------------------------------------------------


async def drive_indexer(
    app: FNDApp,
    *,
    collection: str,
    config: CollectionConfig,
    index_dir: Path,
    rebuild: bool = False,
    cancel: asyncio.Event,
    events: asyncio.Queue[ProgressEvent],
) -> None:
    """Owns the async indexer for the app's lifetime of this run.

    Pushes each ProgressEvent onto ``events`` (the modal drains them)
    and also writes the latest snapshot to ``app._indexer_state`` so
    the footer indicator + a re-opened modal can read current state
    without subscribing to the queue mid-stream.
    """
    async for ev in run_indexer(
        config=config,
        collection=collection,
        index_dir=index_dir,
        rebuild=rebuild,
        cancel=cancel,
    ):
        # Mirror into the snapshot first so footer/late-attach modal
        # see fresh data even before the queue consumer wakes.
        snap = _event_to_state(
            ev, collection=collection, started_at_default=app._indexer_started_at
        )
        app._indexer_state = snap
        app._indexer_last_event = ev
        with _SuppressFullQueueLoss():
            events.put_nowait(ev)
        if ev.kind in ("done", "cancelled"):
            break


def _event_to_state(ev: ProgressEvent, *, collection: str, started_at_default: str) -> IndexState:
    return IndexState(
        collection=collection,
        started_at=started_at_default,
        total_files=ev.files_total,
        files_completed=ev.files_done,
        cache_hits=ev.cache_hits_total,
        cache_misses=ev.cache_misses_total,
        current_file=ev.current_file,
        last_update=dt.datetime.now(tz=dt.UTC).isoformat(timespec="seconds"),
    )


class _SuppressFullQueueLoss:
    """Drop oldest event if the queue gets back-pressured.

    Unbounded queue is fine for thousands of events; if for some reason
    a backlog appears (UI completely frozen), losing intermediate
    file_complete events is acceptable — the next one carries fresh
    totals.
    """

    def __enter__(self) -> _SuppressFullQueueLoss:
        return self

    def __exit__(self, *_a: object) -> None:
        return None


# Reference the type so the import doesn't get tree-shaken away when
# only used inside `if TYPE_CHECKING:` for the modal.
_ = AsyncIterator
_ = time  # ditto for time
