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
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import OptionList, ProgressBar, Static, Tree

from fnd.config import CollectionConfig
from fnd.index_runner import IndexState, ProgressEvent, run_indexer

if TYPE_CHECKING:
    from fnd.tui.app import FNDApp


def _live_elapsed_seconds(started_at_iso: str) -> float:
    """Wall-clock seconds since ``started_at_iso`` (ISO-8601 UTC).
    Returns 0.0 on parse failure so the timing line never goes
    negative."""
    try:
        ts = dt.datetime.fromisoformat(started_at_iso)
    except ValueError:
        return 0.0
    return max(0.0, (dt.datetime.now(tz=dt.UTC) - ts).total_seconds())


def fmt_duration(seconds: float) -> str:
    """Format a duration in seconds as "Ns", "Nm Ms", or "Nh Mm"."""
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


# Compat alias - older callers (tests) imported this as ``fmt_eta``.
fmt_eta = fmt_duration


def invalidate_todo_count_cache() -> None:
    """Drop every cached flat-PDF scan so the next refresh recomputes.
    Called when a chain finishes: a run can resolve files across multiple
    collections, so the unscoped count AND each per-collection scope (read
    by StillFlatDrillIn) are stale — clear all of them."""
    from fnd.tui import flat_pdf_scan

    flat_pdf_scan.invalidate_all()


def _stuck_suffix() -> str:
    """If the current page hasn't progressed in much longer than the
    session avg, append a yellow "stuck Ns on this page" tag so the
    user can tell a wedged file from a merely slow one. Threshold is
    max(30s, 10x avg) - the floor catches genuine wedges on fast docs
    where avg is sub-second; the multiplier scales with naturally slow
    docs (60-page decks where each page legitimately takes 8-10s)."""
    from fnd.tui import live_progress

    since = live_progress.seconds_since_last_beat()
    if since <= 0:
        return ""
    pages, page_secs = live_progress.session_snapshot()
    avg = (page_secs / pages) if pages > 0 else 0.0
    threshold = max(30.0, 10.0 * avg) if avg > 0 else 60.0
    if since < threshold:
        return ""
    return f"   [yellow]· stuck {int(since)}s[/]"


def fmt_per_page(session_pages: int, session_page_seconds: float) -> str:
    """Render avg seconds per page from session counters, or '?'
    when no pages have completed yet (cache-only chain, non-PDFs,
    pre-first-page)."""
    if session_pages <= 0 or session_page_seconds <= 0:
        return "?"
    avg = session_page_seconds / session_pages
    if avg < 10:
        return f"{avg:.1f}s per page"
    return f"{int(avg)}s per page"


class IndexerScreen(ModalScreen[None]):
    """Modal screen showing live reindex progress."""

    BINDINGS = [  # noqa: RUF012
        Binding("escape,b", "background", "Background", show=True),
        Binding("c", "cancel", "Cancel", show=True),
        Binding("p", "pause", "Pause", show=True),
        Binding("f", "show_failed", "Flat PDFs", show=True),
    ]

    CSS = """
    IndexerScreen { align: center middle; background: $surface 75%; }
    #indexer_box {
        width: 75%;
        min-width: 60;
        max-width: 140;
        height: auto;
        max-height: 90%;
        border: round $accent;
        padding: 1 2;
        background: $surface;
    }
    #indexer_history_tree {
        height: auto;
        max-height: 10;
        margin: 0 0 1 0;
        border: none;
        background: $surface;
        color: $text-muted;
    }
    #indexer_history_tree:focus-within { color: $text; }
    #indexer_history_tree.hidden { display: none; }
    #indexer_status, #indexer_pages_label, #indexer_current_file,
    #indexer_timing, #indexer_indexed_line, #indexer_texture_line {
        height: 1;
        padding: 0;
    }
    #indexer_progress, #indexer_pages_progress {
        width: 100%;
        height: 1;
    }
    #indexer_progress { margin: 0 0 1 0; }
    #indexer_pages_progress { margin: 0 0 1 0; }
    #indexer_pages_label.hidden, #indexer_pages_progress.hidden { display: none; }
    #indexer_current_file { margin: 0 0 0 0; }
    #indexer_timing { margin: 1 0 0 0; }
    #indexer_texture_line.hidden { display: none; }
    #indexer_actions_box {
        border: round $primary 50%;
        padding: 0 1;
        margin: 1 0 0 0;
        height: auto;
    }
    #indexer_actions_box:focus-within { border: round $accent; }
    #indexer_actions { height: auto; padding: 0; border: none; background: $surface; }
    """

    def __init__(self, collection: str, *, chain_total: int = 1, chain_index: int = 1) -> None:
        super().__init__()
        self._collection = collection
        self._chain_total = chain_total
        self._chain_index = chain_index
        # IDs of OptionList options that have been added dynamically.
        # Tracked here so _sync_action_options doesn't have to walk
        # OptionList internals (its `_options` attribute is private +
        # may change across Textual versions).
        self._added_options: set[str] = set()
        # Action options that should be REMOVED once the chain
        # finishes (Background + Cancel are meaningless post-Done).
        self._removed_options: set[str] = set()
        # Last-rendered Flat PDFs list count so the option's
        # label can be re-rendered when the count changes (a chain that
        # resolves a wedge should drop the count from N to N-1, not
        # leave the stale label sitting in the option list).
        self._last_todo_count: int | None = None

    def compose(self) -> ComposeResult:
        from textual.widgets import OptionList
        from textual.widgets.option_list import Option

        with Vertical(id="indexer_box") as box:
            box.border_title = self._title_text()
            # Per-collection summary tree. Top-level nodes are finished
            # collections; expanding one reveals its stats inline so the
            # user doesn't have to leave the modal to inspect them.
            # Mirrors the file/matches expand pattern in the main
            # search Results pane.
            history_tree: Tree[str] = Tree("Completed", id="indexer_history_tree")
            history_tree.show_root = True
            # Start collapsed so it doesn't eat vertical space on a
            # fresh chain start, and so the modal's initial focus
            # (the Actions OptionList) stays the primary interaction.
            history_tree.root.collapse()
            history_tree.add_class("hidden")
            yield history_tree
            yield Static("Starting…", id="indexer_status")
            yield ProgressBar(total=1, show_eta=False, show_percentage=True, id="indexer_progress")
            yield Static("", id="indexer_current_file")
            yield Static("", id="indexer_pages_label", classes="hidden")
            yield ProgressBar(
                total=1,
                show_eta=False,
                show_percentage=True,
                id="indexer_pages_progress",
                classes="hidden",
            )
            yield Static("", id="indexer_timing")
            yield Static("", id="indexer_indexed_line")
            yield Static("", id="indexer_texture_line", classes="hidden")
            # Background + Cancel are always available; Summary appears
            # once any collection has finished in this chain; Done
            # appears only when the whole chain has finished. Both are
            # added dynamically via _sync_action_options.
            with Vertical(id="indexer_actions_box") as actions_box:
                actions_box.border_title = "Actions"
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
        # Make sure the option list reflects the current state on a
        # re-attach (Background → click footer indicator → re-open),
        # so a finished run shows "Done" / "View summary" immediately
        # rather than waiting for the 1Hz tick.
        with contextlib.suppress(Exception):
            self._sync_action_options(self._fnd_app())
        # Default focus on the Actions list (the primary interaction).
        # The tree starts collapsed; user can Tab to it then Enter
        # to expand. Without this, focus lands on whichever widget
        # Textual picks first and arrows go to the wrong place.
        with contextlib.suppress(Exception):
            self.query_one("#indexer_actions", OptionList).focus()
        # Kick off the flat-PDF scan on a worker thread so the modal
        # paints now and the "Flat PDFs" count appears when ready. The
        # scan is seconds-long on a real corpus (20-30 s mid-rebuild);
        # running it on the event loop is what froze the portal on open.
        self._schedule_todo_refresh()
        # Spawn a coroutine that drains the event queue and updates
        # the widgets until the screen is dismissed.
        self.run_worker(self._drain_events(), exclusive=False)
        # Live tick: events fire on file boundaries only, so a single
        # slow PDF would otherwise freeze Elapsed and the page-progress
        # bar in the modal for minutes. The 1Hz ticker re-derives both
        # from the latest snapshot the async runner publishes onto the
        # app so the user sees per-page progress and a moving wall
        # clock even when no file has finished in the last second.
        self.set_interval(1.0, self._tick_timing)

    def _apply_latest_state(self) -> None:
        app = self._fnd_app()
        state = app._indexer_state
        if state is None:
            return
        self._apply_state_snapshot(state)

    def _tick_timing(self) -> None:
        """Re-render the Elapsed + per-page lines + pages-bar from the
        latest snapshot. Bound to 1Hz so the user sees time tick by
        between file_complete events.

        Pauses once the chain is fully Done so Elapsed doesn't run
        away after the run has finished - the recorded elapsed_s from
        the done event is the truthful run duration."""
        app = self._fnd_app()
        started_at = app._indexer_started_at
        if not started_at:
            return
        if self._chain_finished(app):
            ev = app._indexer_last_event
            if ev is not None and getattr(ev, "kind", "") in ("done", "cancelled"):
                self._render_timing_from_event(ev)
            # Clear the per-file Pages bar / label - no file is in
            # flight post-chain, so the last "40/40" snapshot would
            # otherwise stay frozen on screen alongside "Done.".
            with contextlib.suppress(Exception):
                self.query_one("#indexer_pages_label", Static).add_class("hidden")
                self.query_one("#indexer_pages_progress", ProgressBar).add_class("hidden")
            return
        ev = app._indexer_last_event
        elapsed_now = _live_elapsed_seconds(started_at)
        if ev is not None:
            elapsed_now = max(elapsed_now, getattr(ev, "elapsed_s", 0.0))
        self._render_pages_progress(app)
        self._render_timing(elapsed_now)
        self._sync_action_options(app)

    def _render_pages_progress(self, app: FNDApp) -> None:
        """Show / update the per-file pages bar + label + Current line.

        Hidden when no per-page snapshot is available (cache hit, non-
        PDF, or before the first page beat). Shown with progress when
        a PDF is mid-extraction."""
        from fnd.tui import live_progress

        ev = app._indexer_last_event
        current_path = (
            getattr(ev, "current_file", "")
            if ev is not None
            else app._indexer_state.current_file
            if app._indexer_state is not None
            else ""
        )
        stuck_suffix = _stuck_suffix()
        with contextlib.suppress(Exception):
            current = self.query_one("#indexer_current_file", Static)
            current.update(f"[dim]Current:[/] {_short_name(current_path)}{stuck_suffix}")
        _path, pages_done, pages_total, _start = live_progress.snapshot()
        try:
            label = self.query_one("#indexer_pages_label", Static)
            bar = self.query_one("#indexer_pages_progress", ProgressBar)
        except Exception:
            return
        if pages_total > 0:
            label.remove_class("hidden")
            bar.remove_class("hidden")
            shown_done = min(pages_done, pages_total)
            label.update(f"[dim]Pages:[/]   {shown_done} / {pages_total}")
            bar.update(total=pages_total, progress=shown_done)
        else:
            label.add_class("hidden")
            bar.add_class("hidden")
            label.update("")

    def _render_timing(self, elapsed_seconds: float) -> None:
        from fnd.tui import live_progress

        pages, page_secs = live_progress.session_snapshot()
        per_page = fmt_per_page(pages, page_secs)
        with contextlib.suppress(Exception):
            timing = self.query_one("#indexer_timing", Static)
            timing.update(
                f"[dim]Elapsed:[/] {fmt_duration(elapsed_seconds)}    "
                f"[dim]·[/]    [dim]Avg:[/] {per_page}"
            )

    def _render_timing_from_event(self, ev: Any) -> None:
        self._render_timing(getattr(ev, "elapsed_s", 0.0))

    def _chain_finished(self, app: FNDApp) -> bool:
        """True when the chain has reached a terminal state - either
        every collection finished (done) or the user cancelled
        (cancelled). Both states should freeze the live timer so the
        displayed elapsed reflects the truthful run duration, not how
        long the user left the modal open afterwards."""
        ev = app._indexer_last_event
        if ev is None:
            return False
        kind = getattr(ev, "kind", "")
        if kind == "cancelled":
            return True
        if kind != "done":
            return False
        pending = getattr(app, "_indexer_chain_remaining", None) or []
        callback_pending = getattr(app, "_indexer_chain_callback_pending", False)
        return not pending and not callback_pending

    def _sync_action_options(self, app: FNDApp) -> None:
        """Show / hide action options based on chain state.

        Active run:  Background, Cancel [, Skip current file if stuck,
                     Flat PDFs list if count > 0]
        Chain done:  Flat PDFs list, Done   (Background +
                     Cancel + Skip removed once the chain finishes)"""
        from textual.widgets import OptionList
        from textual.widgets.option_list import Option

        try:
            opts = self.query_one("#indexer_actions", OptionList)
        except Exception:
            return
        want_done = self._chain_finished(app)
        want_skip = (not want_done) and bool(_stuck_suffix())
        # Read the last background scan; never compute inline (the scan is
        # seconds-long and would freeze the modal). None = not scanned yet,
        # so no to-do option until _on_todo_ready fires.
        from fnd.tui import flat_pdf_scan

        todo_count = flat_pdf_scan.cached_count(None) or 0
        want_todo = todo_count > 0
        if want_skip and "skip" not in self._added_options:
            with contextlib.suppress(Exception):
                opts.add_option(Option("Skip current file", id="skip"))
                self._added_options.add("skip")
        if want_todo and todo_count != self._last_todo_count:
            with contextlib.suppress(Exception):
                if "todo" in self._added_options:
                    opts.remove_option("todo")
                opts.add_option(Option(f"Flat PDFs — review & retry ({todo_count})", id="todo"))
                self._added_options.add("todo")
                self._last_todo_count = todo_count
        elif not want_todo and "todo" in self._added_options:
            # Count dropped to 0 - remove the option so the user
            # doesn't see a now-empty entry.
            with contextlib.suppress(Exception):
                opts.remove_option("todo")
                self._added_options.discard("todo")
                self._last_todo_count = 0
        if want_done and "done" not in self._added_options:
            with contextlib.suppress(Exception):
                opts.add_option(Option("Done", id="done"))
                self._added_options.add("done")
        # Post-done: Background + Cancel + Skip are meaningless. Remove
        # them so the only choices are the to-do (if any) and Done.
        # Tracked in _removed_options so we don't try the remove twice.
        if want_done:
            for opt_id in ("background", "cancel", "skip"):
                if opt_id in self._removed_options:
                    continue
                with contextlib.suppress(Exception):
                    opts.remove_option(opt_id)
                    self._removed_options.add(opt_id)

    def _schedule_todo_refresh(self) -> None:
        """Recompute the flat-PDF count off the event loop and re-sync
        the action options when it lands. Bounded to mount + per-
        collection completion — never the 1Hz tick — so a long rebuild
        doesn't spawn a scan every few seconds that contends with the
        indexer it's measuring."""
        from fnd.tui import flat_pdf_scan

        with contextlib.suppress(Exception):
            flat_pdf_scan.schedule_refresh(self.app, None, on_ready=self._on_todo_ready)

    def _on_todo_ready(self, _rows: Any) -> None:
        """Background scan finished (marshalled onto the UI thread):
        re-sync the option list so the now-known count shows."""
        with contextlib.suppress(Exception):
            self._sync_action_options(self._fnd_app())

    def _refresh_todo_after_run(self) -> None:
        """A collection finished/cancelled: the backlog just changed, so
        drop the stale scan and recompute off-loop for an accurate count."""
        invalidate_todo_count_cache()
        self._schedule_todo_refresh()

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
        except Exception:
            return

        if ev.kind == "enumerating":
            self._refresh_title()
            status.update("[dim]Scanning sources…[/]")
            bar.update(total=100, progress=0)
            current.update("")
            self._render_timing(ev.elapsed_s)
            return
        if ev.kind == "started":
            self._refresh_title()
            status.update(f"[dim]Files:[/]   0 / {ev.files_total}")
            bar.update(total=max(1, ev.files_total), progress=0)
        elif ev.kind == "file_complete":
            status.update(f"[dim]Files:[/]   {ev.files_done} / {ev.files_total}")
            bar.update(progress=ev.files_done)
        elif ev.kind == "cancelled":
            status.update("[yellow]Cancelled.[/] State saved; re-run to resume.")
            self._refresh_todo_after_run()
        elif ev.kind == "done":
            status.update(f"[green]Done.[/]   {ev.files_done} / {ev.files_total} files")
            self._refresh_todo_after_run()

        current.update(f"[dim]Current:[/] {_short_name(ev.current_file)}")
        self._render_timing(ev.elapsed_s)
        self._update_status_lines(
            pdfs_total=ev.pdfs_total,
            indexed_newly=ev.indexed_newly_total,
            indexed_already=ev.indexed_already_total,
            textured_newly=ev.textured_newly_total,
            textured_already=ev.textured_already_total,
            still_flat=ev.still_flat_total,
            failed=ev.failed_total,
        )
        self._sync_action_options(self._fnd_app())

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
        self._refresh_history_band()

    def _refresh_history_band(self) -> None:
        """Sync the per-collection summary Tree to the current chain
        history.

        Layout mirrors the main progress section's two-line format
        (Indexed:..., Texturising:...) sub-sectioned by collection.
        Tree shape:

            ▼ Completed (N)
            ├── ▼ default                                   ← collection
            │   ├── Indexed:     2 newly indexed   28 already indexed
            │   └── Texturising: 0 newly textured  28 already textured  ⚠ 1 still flat
            └── ▼ second
                └── Indexed:     1 newly indexed   0 already indexed

        The root starts collapsed; each collection parent inside is
        auto-expanded so a single Enter on the root reveals every
        collection's stats at once (matching the main section's
        always-visible layout)."""
        try:
            tree: Tree[str] = self.query_one("#indexer_history_tree", Tree)
        except Exception:
            return
        app = self._fnd_app()
        history = getattr(app, "_indexer_chain_history", None) or []
        if not history:
            tree.add_class("hidden")
            tree.root.remove_children()
            return
        tree.remove_class("hidden")
        was_expanded = tree.root.is_expanded
        tree.root.remove_children()
        tree.root.set_label(f"Completed ({len(history)})")
        for snap in history:
            collection_node = tree.root.add(snap.collection, data=snap.collection)
            collection_node.add_leaf(
                _format_indexed_line(snap.indexed_newly, snap.indexed_already, snap.failed)
            )
            if snap.pdfs_total > 0:
                collection_node.add_leaf(
                    _format_texturising_line(
                        snap.textured_newly, snap.textured_already, snap.still_flat
                    )
                )
            collection_node.expand()
        if was_expanded:
            tree.root.expand()

    # ---- bindings ----

    async def action_background(self) -> None:
        """Dismiss the modal; task keeps running on the app."""
        self.dismiss(None)

    async def action_cancel(self) -> None:
        app = self._fnd_app()
        if app._indexer_cancel is not None:
            app._indexer_cancel.set()
        # Setting the event alone isn't enough: the runner only checks
        # cancel.is_set() at file boundaries. A 10-minute PDF in
        # flight means Cancel sits unresponsive for ten minutes. Set
        # the worker's cancel beacon first so the BrokenProcessPool/
        # StallError retry bails after one attempt, THEN tear the
        # pool down so the current extract raises BrokenProcessPool
        # immediately. The next iteration of the runner's loop sees
        # cancel.is_set() and exits with the "cancelled" event the
        # modal drains.
        with contextlib.suppress(Exception):
            from fnd.extract._worker import (
                _get_pool,
                _kill_pool_workers,
                request_cancel,
                shutdown_pool,
            )

            request_cancel()
            pool = _get_pool()
            _kill_pool_workers(pool)
            shutdown_pool()
        with contextlib.suppress(Exception):
            self.query_one("#indexer_status", Static).update(
                "[yellow]Cancelling…[/] waiting for current file to abort."
            )

    async def action_pause(self) -> None:
        """Pause is conceptually the same as Cancel for now.
        Re-running the reindex resumes via cache + state file."""
        await self.action_cancel()

    async def action_skip(self) -> None:
        """Skip the current file and continue the chain.

        Same kill-and-bypass-retry mechanism as Cancel, but does NOT
        set the chain-wide cancel event - the runner sees the worker
        die, marks this file as failed, and moves to the next file.
        Visible only when the 'stuck' indicator fires."""
        with contextlib.suppress(Exception):
            from fnd.extract._worker import (
                _get_pool,
                _kill_pool_workers,
                request_cancel,
                shutdown_pool,
            )

            request_cancel()
            pool = _get_pool()
            _kill_pool_workers(pool)
            shutdown_pool()
        with contextlib.suppress(Exception):
            self.query_one("#indexer_status", Static).update("[yellow]Skipping current file…[/]")

    def action_show_failed(self) -> None:
        """Open the still-flat / failed PDFs drill-in for the current
        collection. Scoped to the active collection when running a
        single Update; unscoped when invoked mid-chain since the chain
        cycles through multiple collections."""
        from fnd.tui.settings_screen import StillFlatDrillIn

        scope = self._collection if self._chain_total <= 1 else None
        self.app.push_screen(StillFlatDrillIn(collection=scope))

    def action_done(self) -> None:
        """Close the indexer modal. Identical to Background except it
        also resets the chain-history snapshot so the next Update-all
        starts with a clean slate."""
        app = self._fnd_app()
        app._indexer_chain_history = []  # type: ignore[attr-defined]
        self.dismiss(None)

    # ---- OptionList action dispatch ----

    async def on_option_list_option_selected(self, ev: Any) -> None:
        if ev.option.id == "background":
            await self.action_background()
        elif ev.option.id == "done":
            self.action_done()
        elif ev.option.id == "cancel":
            await self.action_cancel()
        elif ev.option.id == "skip":
            await self.action_skip()
        elif ev.option.id == "todo":
            self.action_show_failed()

    # ---- arrow-key crossover between Tree and Actions ----

    def on_key(self, event: events.Key) -> None:
        """Let arrows hop between the per-collection summary Tree and
        the Actions list so the modal feels like one continuous list
        instead of two disjoint focus contexts. Standard Tab still
        works; this just adds the cheaper-to-discover up/down path.

        Also adds Right/Left expand/collapse on the Tree (Textual's
        default Tree bindings only have enter/space; the user expects
        right/left like every other tree widget in the app)."""
        if event.key not in ("up", "down", "right", "left"):
            return
        # Right/Left on the Tree → expand/collapse cursor node.
        if event.key in ("right", "left"):
            focused = self.focused
            try:
                tree = self.query_one("#indexer_history_tree", Tree)
            except Exception:
                return
            if focused is not tree:
                return
            cursor = tree.cursor_node
            if cursor is None:
                return
            if event.key == "right":
                cursor.expand()
            else:
                cursor.collapse()
            event.stop()
            return
        focused = self.focused
        if focused is None:
            return
        try:
            tree = self.query_one("#indexer_history_tree", Tree)
            actions = self.query_one("#indexer_actions", OptionList)
        except Exception:
            return
        if focused is actions:
            # Up at the first option → focus the Tree (only if it
            # has any history to navigate; otherwise stay put).
            at_top = getattr(actions, "highlighted", None) == 0
            tree_visible = "hidden" not in tree.classes
            if event.key == "up" and at_top and tree_visible:
                tree.focus()
                event.stop()
        elif focused is tree:
            # Down at the last visible row (or at the collapsed root)
            # → focus the Actions list.
            cursor = tree.cursor_node
            at_root_collapsed = cursor is tree.root and not tree.root.is_expanded
            at_last_leaf = (
                tree.root.is_expanded
                and bool(tree.root.children)
                and cursor is tree.root.children[-1]
            )
            if event.key == "down" and (at_root_collapsed or at_last_leaf):
                actions.focus()
                event.stop()


@dataclass(slots=True, frozen=True)
class ChainStepSummary:
    """Frozen per-collection snapshot captured when a chain step
    completes. Drives the in-modal per-collection summary Tree so the
    user never loses sight of what each collection produced just
    because the chain moved on."""

    collection: str
    files_total: int
    pdfs_total: int
    indexed_newly: int
    indexed_already: int
    textured_newly: int
    textured_already: int
    still_flat: int
    failed: int
    elapsed_s: float


def _short_name(path: str) -> str:
    if not path:
        return "?"
    name = Path(path).name
    return name if len(name) <= 68 else name[:65] + "…"


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
    skip_unchanged: bool = True,
    force_fresh: bool = False,
    run_seq: int = 0,
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
        skip_unchanged=skip_unchanged,
        force_fresh=force_fresh,
    )
    final_event: Any = None
    try:
        async for ev in gen:
            snap = _event_to_state(
                ev, collection=collection, started_at_default=app._indexer_started_at
            )
            app._indexer_state = snap
            app._indexer_last_event = ev
            with _SuppressFullQueueLoss():
                events.put_nowait(ev)
            if ev.kind in ("done", "cancelled"):
                final_event = ev
                break
    finally:
        await gen.aclose()  # type: ignore[attr-defined]

    if final_event is not None and final_event.kind == "done":
        history: list[Any] = getattr(app, "_indexer_chain_history", None) or []
        history.append(
            ChainStepSummary(
                collection=collection,
                files_total=final_event.files_total,
                pdfs_total=final_event.pdfs_total,
                indexed_newly=final_event.indexed_newly_total,
                indexed_already=final_event.indexed_already_total,
                textured_newly=final_event.textured_newly_total,
                textured_already=final_event.textured_already_total,
                still_flat=final_event.still_flat_total,
                failed=final_event.failed_total,
                elapsed_s=final_event.elapsed_s,
            )
        )
        app._indexer_chain_history = history  # type: ignore[attr-defined]

    # A superseded run — a newer explicit run bumped the generation while
    # this one was winding down (e.g. cancel-then-Rebuild-all) — must not
    # touch the shared chain state, or its late teardown would clobber the
    # queue the newer run just set up. The newer run owns the chain now.
    if getattr(app, "_indexer_run_seq", run_seq) != run_seq:
        return

    pending: list[str] = getattr(app, "_indexer_chain_remaining", None) or []
    if pending and not cancel.is_set():
        next_collection = pending.pop(0)
        app._indexer_chain_remaining = pending  # type: ignore[attr-defined]
        app._indexer_chain_callback_pending = True  # type: ignore[attr-defined]
        app.call_later(_start_next_in_chain, app, next_collection)
    else:
        app._indexer_chain_remaining = []  # type: ignore[attr-defined]
        app._indexer_chain_total = 1  # type: ignore[attr-defined]
        app._indexer_chain_callback_pending = False  # type: ignore[attr-defined]
        # No auto-push of a separate summary screen at chain end - the
        # in-modal Done tree carries the same data inline and the
        # user explicitly didn't want a screen swap after indexing.
        # Invalidate the to-do cache so the modal's Files-needing-
        # attention count reflects the chain's just-resolved files
        # without waiting for the 5s TTL.
        invalidate_todo_count_cache()
        # Refresh the live searcher off the just-committed generation and
        # re-run the active query so newly-indexed files show up without
        # the user retyping. _run_query also reloads per-query as a
        # backstop, but this updates already-displayed results in place.
        app.call_later(app._on_reindex_complete)


def _start_next_in_chain(app: FNDApp, collection: str) -> None:
    """Continuation that fires the next collection's Update index.
    Lives at module scope so the closure capture is explicit and
    pyright can type-check the call site."""
    from fnd.config import load as _load_config

    app._indexer_chain_callback_pending = False  # type: ignore[attr-defined]
    in_memory_cfg = getattr(app, "_config", None)
    cfg = in_memory_cfg if in_memory_cfg is not None else _load_config()
    if collection not in cfg.collections:
        return
    col_cfg = cfg.collections[collection]
    app._indexer_task = None  # type: ignore[attr-defined] # release
    override = getattr(app, "_indexer_texturise_override", None)
    skip_unchanged = getattr(app, "_indexer_skip_unchanged", True)
    force_fresh = getattr(app, "_indexer_force_fresh", False)
    rebuild = getattr(app, "_indexer_rebuild", False)
    app.start_indexer(
        collection=collection,
        config=col_cfg,
        open_modal=False,
        rebuild=rebuild,
        texturise_override=override,
        skip_unchanged=skip_unchanged,
        force_fresh=force_fresh,
        _bump_seq=False,  # chain continuation inherits the run generation
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
