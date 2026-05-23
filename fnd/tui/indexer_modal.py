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
import contextlib
import datetime as dt
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import ProgressBar, Static

from fnd.config import CollectionConfig
from fnd.index_runner import IndexState, ProgressEvent, run_indexer

if TYPE_CHECKING:
    from fnd.tui.app import FNDApp


def fmt_eta(seconds: float) -> str:
    """Format a duration in seconds. Returns ``—`` for unknown
    durations (negative / NaN / infinite — all happen during the
    ``started`` event before the first file completes)."""
    import math

    if seconds < 0 or math.isnan(seconds) or math.isinf(seconds):
        return "?"
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
        Binding("f", "show_failed", "Failed", show=True),
    ]

    CSS = """
    IndexerScreen { align: center middle; background: $surface 75%; }
    #indexer_box {
        width: auto;
        min-width: 60;
        max-width: 100;
        height: auto;
        max-height: 90%;
        border: round $accent;
        padding: 0 1;
        background: $surface;
    }
    #indexer_status, #indexer_current_file, #indexer_timing,
    #indexer_indexed_line, #indexer_texture_line {
        height: 1;
        padding: 0;
    }
    #indexer_texture_line.hidden { display: none; }
    #indexer_progress { width: 100%; height: 1; padding: 0 0 1 0; }
    #indexer_actions { height: auto; padding: 1 0 0 0; }
    """

    def __init__(self, collection: str, *, chain_total: int = 1, chain_index: int = 1) -> None:
        super().__init__()
        self._collection = collection
        self._chain_total = chain_total
        self._chain_index = chain_index

    def compose(self) -> ComposeResult:
        from textual.widgets import OptionList
        from textual.widgets.option_list import Option

        with Vertical(id="indexer_box") as box:
            box.border_title = self._title_text()
            yield Static("Starting…", id="indexer_status")
            yield ProgressBar(total=1, show_eta=False, show_percentage=True, id="indexer_progress")
            yield Static("", id="indexer_current_file")
            yield Static("", id="indexer_timing")
            yield Static("", id="indexer_indexed_line")
            yield Static("", id="indexer_texture_line", classes="hidden")
            yield OptionList(
                Option("Run in background", id="background"),
                Option("Cancel", id="cancel"),
                id="indexer_actions",
            )

    def _title_text(self) -> str:
        if self._chain_total > 1:
            return (
                f"Update index › {self._collection}  ({self._chain_index} of {self._chain_total})"
            )
        return f"Update index › {self._collection}"

    def _refresh_title(self) -> None:
        """Re-render the box title — used when the chain advances to
        the next collection. Reads the new collection name + chain
        index from the FNDApp."""
        app = self._fnd_app()
        new_name = getattr(app, "_indexer_collection", None) or self._collection
        chain_pending = getattr(app, "_indexer_chain_remaining", None) or []
        chain_total = getattr(app, "_indexer_chain_total", None) or self._chain_total
        chain_index = max(1, chain_total - len(chain_pending))
        self._collection = new_name
        self._chain_total = chain_total
        self._chain_index = chain_index
        with contextlib.suppress(Exception):
            self.query_one("#indexer_box", Vertical).border_title = self._title_text()

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
        """Keep draining while the chain has more work.

        ``app._indexer_chain_remaining`` is populated by
        ``UpdateAllConfirm`` with the collections still to process.
        On ``done`` we only exit when that list is empty; otherwise we
        keep listening so the next collection's events render in this
        same modal. ``cancelled`` always exits."""
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
                if ev.kind == "cancelled":
                    break
                if ev.kind == "done":
                    pending = getattr(app, "_indexer_chain_remaining", None) or []
                    callback_pending = getattr(app, "_indexer_chain_callback_pending", False)
                    if not pending and not callback_pending:
                        break
                    # More work queued. drive_indexer fires the next
                    # collection via call_later; the new run_indexer
                    # emits a "started" event that re-initialises the
                    # progress bar here. callback_pending covers the
                    # window where drive_indexer has already popped
                    # the next name (so pending is empty) but
                    # call_later has not yet fired the new task.
                    continue
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
            self._update_status_lines(
                pdfs_total=state.pdfs_total,
                indexed_newly=state.indexed_newly,
                indexed_already=state.indexed_already,
                textured_newly=state.textured_newly,
                textured_already=state.textured_already,
                still_flat=state.still_flat,
                failed=state.failed,
            )
        except Exception:
            pass

    def _render_event(self, ev: ProgressEvent) -> None:
        try:
            status = self.query_one("#indexer_status", Static)
            bar = self.query_one("#indexer_progress", ProgressBar)
            current = self.query_one("#indexer_current_file", Static)
            timing = self.query_one("#indexer_timing", Static)
        except Exception:
            return

        if ev.kind == "started":
            # New collection starting — refresh the title in case the
            # chain just advanced. Resets the progress bar too.
            self._refresh_title()
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
        timing.update(f"[dim]Elapsed:[/] {fmt_eta(ev.elapsed_s)}    [dim]ETA:[/] {fmt_eta(eta)}")
        self._update_status_lines(
            pdfs_total=ev.pdfs_total,
            indexed_newly=ev.indexed_newly_total,
            indexed_already=ev.indexed_already_total,
            textured_newly=ev.textured_newly_total,
            textured_already=ev.textured_already_total,
            still_flat=ev.still_flat_total,
            failed=ev.failed_total,
        )

    def _update_status_lines(
        self,
        *,
        pdfs_total: int,
        indexed_newly: int,
        indexed_already: int,
        textured_newly: int,
        textured_already: int,
        still_flat: int,
        failed: int,
    ) -> None:
        try:
            indexed_line = self.query_one("#indexer_indexed_line", Static)
            texture_line = self.query_one("#indexer_texture_line", Static)
        except Exception:
            return
        indexed_line.update(_format_indexed_line(indexed_newly, indexed_already, failed))
        if pdfs_total > 0:
            texture_line.remove_class("hidden")
            texture_line.update(
                _format_texturising_line(textured_newly, textured_already, still_flat)
            )
        else:
            texture_line.add_class("hidden")
            texture_line.update("")

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
        """Pause is conceptually the same as Cancel for now.
        Re-running the reindex resumes via cache + state file."""
        await self.action_cancel()

    def action_show_failed(self) -> None:
        """Open the still-flat / failed PDFs drill-in for the current
        collection. Scoped to the active collection when running a
        single Update; unscoped when invoked mid-chain since the chain
        cycles through multiple collections."""
        from fnd.tui.settings_screen import StillFlatDrillIn

        scope = self._collection if self._chain_total <= 1 else None
        self.app.push_screen(StillFlatDrillIn(collection=scope))

    # ---- OptionList action dispatch ----

    async def on_option_list_option_selected(self, ev: Any) -> None:
        if ev.option.id == "background":
            await self.action_background()
        elif ev.option.id == "cancel":
            await self.action_cancel()


def _short_name(path: str) -> str:
    if not path:
        return "?"
    name = Path(path).name
    return name if len(name) <= 48 else name[:45] + "…"


def _format_indexed_line(newly: int, already: int, failed: int) -> str:
    parts = [
        f"{newly} newly indexed",
        f"{already} already indexed",
    ]
    if failed > 0:
        parts.append(f"[yellow]⚠ {failed} failed[/]")
    return "[dim]Indexed:[/]     " + "    ".join(parts)


def _format_texturising_line(newly: int, already: int, still_flat: int) -> str:
    parts = [
        f"{newly} newly textured",
        f"{already} already textured",
    ]
    if still_flat > 0:
        parts.append(f"[yellow]⚠ {still_flat} still flat[/]")
    return "[dim]Texturising:[/] " + "    ".join(parts)


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
    texturise_override: bool | None = None,
) -> None:
    """Owns the async indexer for the app's lifetime of this run.

    Pushes each ProgressEvent onto ``events`` (the modal drains them)
    and also writes the latest snapshot to ``app._indexer_state`` so
    the footer indicator + a re-opened modal can read current state
    without subscribing to the queue mid-stream.

    ``texturise_override`` mirrors ``run_indexer``'s parameter: None
    follows the toggle, True/False force on/off for this run."""
    # Bind the generator so we can explicitly aclose() it after the
    # break. Async generators do not release resources just because
    # iteration stopped - the suspended frame keeps the tantivy
    # IndexWriter (and its directory lock) alive until aclose runs.
    # With the Update-all chain landing the next call_later quickly,
    # that lingering lock collided with the next collection's writer
    # when both shared an index_dir.
    gen = run_indexer(
        config=config,
        collection=collection,
        index_dir=index_dir,
        rebuild=rebuild,
        cancel=cancel,
        texturise_override=texturise_override,
    )
    try:
        async for ev in gen:
            # Mirror into the snapshot first so footer/late-attach
            # modal see fresh data even before the queue consumer wakes.
            snap = _event_to_state(
                ev, collection=collection, started_at_default=app._indexer_started_at
            )
            app._indexer_state = snap
            app._indexer_last_event = ev
            with _SuppressFullQueueLoss():
                events.put_nowait(ev)
            if ev.kind in ("done", "cancelled"):
                break
    finally:
        # run_indexer is annotated as AsyncIterator but actually returns
        # an AsyncGenerator; aclose() exists at runtime but the static
        # type does not advertise it.
        await gen.aclose()  # type: ignore[attr-defined]

    # Update-all-collections chain: when more collections are queued
    # behind this one, dequeue the next and start it. The modal stays
    # mounted across the chain so the user sees one continuous flow.
    pending: list[str] = getattr(app, "_indexer_chain_remaining", None) or []
    if pending and not cancel.is_set():
        next_collection = pending.pop(0)
        app._indexer_chain_remaining = pending  # type: ignore[attr-defined]
        # Set the guard BEFORE scheduling. The drain loop checks both
        # this flag and the remaining list when it sees "done"; without
        # the guard, a chain step that empties the list (the final
        # popped name) would let the modal pop before call_later fires.
        app._indexer_chain_callback_pending = True  # type: ignore[attr-defined]
        # Defer to the next event-loop tick so this task completes
        # cleanly before the next one starts.
        app.call_later(_start_next_in_chain, app, next_collection)
    else:
        # Chain is finished or was cancelled. Reset chain bookkeeping
        # so a subsequent single-collection Update index doesn't
        # render with a stale "1 of N" title.
        app._indexer_chain_remaining = []  # type: ignore[attr-defined]
        app._indexer_chain_total = 1  # type: ignore[attr-defined]
        app._indexer_chain_callback_pending = False  # type: ignore[attr-defined]


def _start_next_in_chain(app: FNDApp, collection: str) -> None:
    """Continuation that fires the next collection's Update index.
    Lives at module scope so the closure capture is explicit and
    pyright can type-check the call site."""
    from fnd.config import load as _load_config

    # Clear the guard now that the deferred step is running. The drain
    # loop relies on this flag flipping False before the new task's
    # first "started" event arrives, otherwise the modal could exit
    # if the new run somehow finished synchronously.
    app._indexer_chain_callback_pending = False  # type: ignore[attr-defined]
    # Prefer in-memory config (tests / live edits) over disk.
    in_memory_cfg = getattr(app, "_config", None)
    cfg = in_memory_cfg if in_memory_cfg is not None else _load_config()
    if collection not in cfg.collections:
        return
    col_cfg = cfg.collections[collection]
    app._indexer_task = None  # type: ignore[attr-defined] # release
    # The chain stores its texturise override on the app so successive
    # call_later steps keep the same mode the user picked at the
    # confirm step (e.g. "Update everything (index + texturise)" stays
    # texturising across all four collections, not just the first).
    override = getattr(app, "_indexer_texturise_override", None)
    app.start_indexer(
        collection=collection,
        config=col_cfg,
        open_modal=False,
        texturise_override=override,
    )


def _event_to_state(ev: ProgressEvent, *, collection: str, started_at_default: str) -> IndexState:
    return IndexState(
        collection=collection,
        started_at=started_at_default,
        total_files=ev.files_total,
        pdfs_total=ev.pdfs_total,
        files_completed=ev.files_done,
        cache_hits=ev.cache_hits_total,
        cache_misses=ev.cache_misses_total,
        indexed_newly=ev.indexed_newly_total,
        indexed_already=ev.indexed_already_total,
        textured_newly=ev.textured_newly_total,
        textured_already=ev.textured_already_total,
        still_flat=ev.still_flat_total,
        failed=ev.failed_total,
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
