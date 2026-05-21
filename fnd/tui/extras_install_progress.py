"""Modal that runs the structured-PDF install / uninstall subprocess
chain with live progress, cancel, and background dismissal.

Follows :class:`fnd.tui.indexer_modal.IndexerScreen` discipline:

- Task and subprocess live on :class:`fnd.tui.app.FNDApp`, not the
  screen, so dismissing (Background) keeps work going.
- Cancel sends SIGTERM to the active subprocess; subsequent commands
  in the chain don't run.
- Progress is derived from stderr / stdout lines emitted by uv —
  parsed loosely (uv prints lines like ``Resolved 23 packages``,
  ``Prepared 5 packages``, ``Installed 5 packages`` we surface for the
  user) and falls back to the most recent line as a status string.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import ProgressBar, Static

if TYPE_CHECKING:
    from fnd.tui.app import FNDApp


Phase = Literal["starting", "running", "done", "cancelled", "failed"]


@dataclass
class ProgressEvent:
    """One unit of progress streamed from the worker to the modal.

    ``cmd_index`` is 0-based for the active command in the chain;
    ``cmd_total`` is the total command count (we run ``uv sync`` then
    ``uv tool install`` for the install chain).
    """

    phase: Phase
    cmd_index: int
    cmd_total: int
    line: str = ""
    error: str = ""


class ExtrasInstallProgressScreen(ModalScreen[None]):
    """Modal that drains the events queue on ``FNDApp._extras_events``.

    Reopening the screen reattaches to the live task — the task
    survives modal dismissal."""

    BINDINGS = [  # noqa: RUF012
        Binding("escape,b", "background_or_close", "Background", show=True),
        Binding("c", "cancel", "Cancel", show=True),
        Binding("enter", "close_if_terminal", "Close", show=False),
    ]

    # Visual consistency with the rest of fnd's UI: brackets reserved
    # for actions in the settings rows render exactly like the
    # ``[ Run ]`` affordance. The Textual ``Button`` widget adds
    # ``▔``/``▁`` decorative chrome that's wider than its label —
    # never matched fnd's settings-row buttons. Use plain Statics.
    #
    # All three button states (Background / Cancel / Close) are
    # composed up-front and toggled via the ``-hidden`` class. Swapping
    # children at runtime hits a remove/mount race where the new
    # widget never appears in the rendered tree.
    CSS = """
    ExtrasInstallProgressScreen { align: center middle; background: $surface 75%; }
    #extras_box {
        width: 76;
        height: auto;
        min-height: 11;
        border: round $accent;
        padding: 1 2;
        background: $surface;
    }
    #extras_box > Static { height: auto; padding: 0 0 1 0; }
    #extras_progress { width: 100%; height: 1; padding: 0 0 1 0; }

    #extras_buttons_running, #extras_buttons_terminal {
        width: 100%;
        height: 1;
        margin: 1 0 0 0;
        color: $accent;
        text-style: bold;
    }
    .-hidden { display: none; }

    #extras_footer {
        dock: bottom;
        height: 1;
        padding: 0 1;
        background: $surface;
        color: $text-muted;
    }
    """

    def __init__(self, *, action_label: str) -> None:
        super().__init__()
        self._action_label = action_label
        self._is_terminal = False  # set True after done/cancelled/failed

    def compose(self) -> ComposeResult:
        with Vertical(id="extras_box"):
            yield Static(f"[bold]{self._action_label} pdf-structure[/]", id="extras_title")
            yield Static("Starting…", id="extras_status")
            yield ProgressBar(total=1, show_eta=False, show_percentage=True, id="extras_progress")
            yield Static(
                "[ Background ]   [ Cancel ]",
                id="extras_buttons_running",
                markup=False,
            )
            yield Static(
                "[ Close ]",
                id="extras_buttons_terminal",
                classes="-hidden",
                markup=False,
            )
        yield Static(
            "[dim]Esc / b[/] Background   [dim]c[/] Cancel",
            id="extras_footer",
        )

    async def on_mount(self) -> None:
        self._apply_latest()
        self.run_worker(self._drain(), exclusive=False)

    def _fnd_app(self) -> FNDApp:
        from fnd.tui.app import FNDApp as _FNDApp

        assert isinstance(self.app, _FNDApp)
        return self.app

    def _apply_latest(self) -> None:
        app = self._fnd_app()
        ev = getattr(app, "_extras_last_event", None)
        if ev is not None:
            self._render_event(ev)

    async def _drain(self) -> None:
        app = self._fnd_app()
        queue = getattr(app, "_extras_events", None)
        if queue is None:
            return
        try:
            while self.is_active:
                try:
                    ev = await asyncio.wait_for(queue.get(), timeout=0.25)
                except TimeoutError:
                    continue
                self._render_event(ev)
                if ev.phase in ("done", "cancelled", "failed"):
                    break
        except asyncio.CancelledError:
            return

    def _render_event(self, ev: ProgressEvent) -> None:
        try:
            status = self.query_one("#extras_status", Static)
            bar = self.query_one("#extras_progress", ProgressBar)
        except Exception:
            return
        total = max(1, ev.cmd_total)
        bar.update(total=total, progress=min(ev.cmd_index, total))
        verb = self._action_label  # "Install" / "Uninstall"
        if ev.phase == "starting":
            status.update(f"Step {ev.cmd_index + 1} of {total}: starting…")
        elif ev.phase == "running":
            status.update(f"Step {ev.cmd_index + 1} of {total}: running")
        elif ev.phase == "done":
            # Tailor completion copy to action so it never reads as
            # "Restart fnd to use it" on an uninstall (nothing to use).
            if verb.lower().startswith("install"):
                tail = "Restart fnd, then run Update index to populate the cache."
            elif verb.lower().startswith("uninstall"):
                tail = "Restart fnd to apply."
            else:
                tail = "Restart fnd to apply."
            status.update(f"[bold green]✓ {verb} complete.[/]  {tail}")
            bar.update(progress=total)
            self._enter_terminal_state()
        elif ev.phase == "cancelled":
            status.update("[bold yellow]Cancelled.[/]  Re-run to resume.")
            self._enter_terminal_state()
        elif ev.phase == "failed":
            status.update(f"[bold red]✗ {verb} failed.[/]  {ev.error}")
            self._enter_terminal_state()
        # Raw subprocess stdout (e.g. "- tabulate==0.10.0") would
        # leak implementation detail into the user-facing modal. We
        # intentionally do not surface it; the status line + progress
        # bar are enough signal.

    def _enter_terminal_state(self) -> None:
        """Hide the Background/Cancel row, reveal the Close row.
        Toggling pre-composed Statics is race-free — earlier attempts
        to swap widgets via mount/remove hit a tick-ordering bug that
        left the modal with no buttons."""
        if self._is_terminal:
            return
        self._is_terminal = True
        try:
            self.query_one("#extras_buttons_running", Static).add_class("-hidden")
            self.query_one("#extras_buttons_terminal", Static).remove_class("-hidden")
            self.query_one("#extras_footer", Static).update("[dim]⏎ / Esc[/] Close")
        except Exception:
            pass

    async def action_background(self) -> None:
        self.dismiss(None)

    async def action_cancel(self) -> None:
        app = self._fnd_app()
        cancel = getattr(app, "_extras_cancel", None)
        if cancel is not None:
            cancel.set()
        proc = getattr(app, "_extras_proc", None)
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.send_signal(signal.SIGTERM)

    async def on_click(self, _ev: events.Click) -> None:
        """The button rows are plain Statics, not Buttons — click is
        not the primary interaction (key bindings do the work) but
        for terminal-state we still want clicks on the bottom row to
        dismiss. Mouse handling is best-effort."""
        return

    async def action_background_or_close(self) -> None:
        """Esc / b behaves as Close once the operation has finished —
        the modal has no work to background at that point."""
        if self._is_terminal:
            self.dismiss(None)
            return
        await self.action_background()

    async def action_close_if_terminal(self) -> None:
        """Enter closes the modal only after the operation has
        finished. While running, Enter is a no-op so a stray keystroke
        doesn't dismiss in-flight work."""
        if self._is_terminal:
            self.dismiss(None)


# ── Worker ──────────────────────────────────────────────────────────


async def run_install(
    app: FNDApp,
    *,
    cmds: list[list[str]],
    cancel: asyncio.Event,
    events: asyncio.Queue[ProgressEvent],
) -> None:
    """Run ``cmds`` sequentially. Push a ProgressEvent for each phase
    boundary and for every stderr line (which uv uses for its
    progress / installed lines).

    Cancellation: setting ``cancel`` sends SIGTERM to the active
    subprocess; the chain stops at the next boundary.
    """
    total = len(cmds)
    for i, argv in enumerate(cmds):
        events.put_nowait(
            ProgressEvent(phase="starting", cmd_index=i, cmd_total=total, line=" ".join(argv))
        )
        if cancel.is_set():
            events.put_nowait(ProgressEvent(phase="cancelled", cmd_index=i, cmd_total=total))
            return
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except FileNotFoundError as e:
            events.put_nowait(
                ProgressEvent(
                    phase="failed", cmd_index=i, cmd_total=total, error=str(e), line=" ".join(argv)
                )
            )
            return
        app._extras_proc = proc  # type: ignore[attr-defined]
        events.put_nowait(ProgressEvent(phase="running", cmd_index=i, cmd_total=total))
        assert proc.stdout is not None
        async for raw in proc.stdout:
            text = raw.decode("utf-8", errors="replace").rstrip()
            if text:
                events.put_nowait(
                    ProgressEvent(phase="running", cmd_index=i, cmd_total=total, line=text)
                )
        rc = await proc.wait()
        app._extras_proc = None  # type: ignore[attr-defined]
        if cancel.is_set():
            events.put_nowait(ProgressEvent(phase="cancelled", cmd_index=i, cmd_total=total))
            return
        if rc != 0:
            events.put_nowait(
                ProgressEvent(
                    phase="failed",
                    cmd_index=i,
                    cmd_total=total,
                    error=f"exit {rc}",
                )
            )
            return
    events.put_nowait(ProgressEvent(phase="done", cmd_index=total - 1, cmd_total=total))


def start_extras_install(
    app: FNDApp,
    *,
    cmds: list[list[str]],
    action_label: str,
) -> bool:
    """Spawn the install/uninstall task and push the progress modal.

    Idempotent — refuses to start a second run while one is in flight,
    re-attaches the modal to the running task instead.
    """
    existing = getattr(app, "_extras_task", None)
    if existing is not None and not existing.done():
        app.push_screen(ExtrasInstallProgressScreen(action_label=action_label))
        return False
    app._extras_cancel = asyncio.Event()  # type: ignore[attr-defined]
    app._extras_events = asyncio.Queue()  # type: ignore[attr-defined]
    app._extras_last_event = None  # type: ignore[attr-defined]
    app._extras_proc = None  # type: ignore[attr-defined]
    app._extras_action_label = action_label  # type: ignore[attr-defined]
    app._extras_task = asyncio.create_task(  # type: ignore[attr-defined]
        run_install(
            app,
            cmds=cmds,
            cancel=app._extras_cancel,  # type: ignore[attr-defined]
            events=app._extras_events,  # type: ignore[attr-defined]
        )
    )
    app.push_screen(ExtrasInstallProgressScreen(action_label=action_label))
    return True


__all__ = [
    "ExtrasInstallProgressScreen",
    "ProgressEvent",
    "run_install",
    "start_extras_install",
]
