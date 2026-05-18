"""App-level progress strip + session API.

One widget at the bottom of the layout drives every long wait. Callers
open a ``ProgressSession`` for the duration of their work; most-recent
session wins. Determinate only — indeterminate mode painted red and the
animation drew the eye on every short load.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Any

from textual.containers import Horizontal
from textual.widget import Widget
from textual.widgets import Label, ProgressBar

if TYPE_CHECKING:
    from textual.app import App


class FNDProgressBar(Widget):
    DEFAULT_CSS = """
    FNDProgressBar {
        layout: horizontal;
        height: 1;
        width: 100%;
        padding: 0 1;
        background: transparent;
    }
    /* visibility:hidden keeps the row, so toggling never reflows the panes above. */
    FNDProgressBar.-idle { visibility: hidden; }
    FNDProgressBar > #progress_phase {
        width: auto;
        min-width: 16;
        color: $text-muted;
        padding: 0 1 0 0;
    }
    FNDProgressBar > Horizontal#progress_bar_wrap {
        width: 1fr;
        height: 1;
    }
    FNDProgressBar Bar > .bar--bar           { color: $accent; }
    FNDProgressBar Bar > .bar--indeterminate { color: $accent; }
    FNDProgressBar Bar > .bar--complete      { color: $success; }
    """

    def __init__(self) -> None:
        super().__init__(id="fnd_progress", classes="-idle")

    def compose(self):  # type: ignore[no-untyped-def]
        yield Label("", id="progress_phase")
        with Horizontal(id="progress_bar_wrap"):
            yield ProgressBar(
                total=1,
                show_eta=False,
                show_percentage=True,
                id="fnd_progress_bar",
            )

    def show(self) -> None:
        self.remove_class("-idle")

    def hide(self) -> None:
        self.add_class("-idle")

    def set_phase(self, label: str) -> None:
        with contextlib.suppress(Exception):
            self.query_one("#progress_phase", Label).update(label)

    def set_total(self, total: int) -> None:
        with contextlib.suppress(Exception):
            bar = self.query_one("#fnd_progress_bar", ProgressBar)
            bar.update(total=max(1, total), progress=min(bar.progress, max(1, total)))

    def set_progress(self, progress: int) -> None:
        with contextlib.suppress(Exception):
            self.query_one("#fnd_progress_bar", ProgressBar).update(progress=progress)

    def reset(self) -> None:
        with contextlib.suppress(Exception):
            self.query_one("#fnd_progress_bar", ProgressBar).update(total=1, progress=0)
            self.query_one("#progress_phase", Label).update("")


class ProgressSession:
    """Handle for one long-running wait. Use as a context manager."""

    def __init__(self, facility: ProgressFacility, *, phase: str, total: int) -> None:
        self._facility = facility
        self._phase = phase
        self._total = max(1, total)
        self._progress = 0
        self._closed = False

    @property
    def phase(self) -> str:
        return self._phase

    @property
    def total(self) -> int:
        return self._total

    @property
    def progress(self) -> int:
        return self._progress

    @property
    def closed(self) -> bool:
        return self._closed

    def set_phase(self, label: str) -> None:
        if self._closed:
            return
        self._phase = label
        self._facility._on_session_update(self)

    def set_total(self, total: int) -> None:
        if self._closed:
            return
        self._total = max(1, total)
        if self._progress > self._total:
            self._progress = self._total
        self._facility._on_session_update(self)

    def set_progress(self, progress: int) -> None:
        if self._closed:
            return
        self._progress = max(0, min(progress, self._total))
        self._facility._on_session_update(self)

    def advance(self, units: int = 1) -> None:
        if self._closed:
            return
        self._progress = min(self._progress + units, self._total)
        self._facility._on_session_update(self)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._facility._on_session_close(self)

    def __enter__(self) -> ProgressSession:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # type: ignore[no-untyped-def]
        self.close()


class ProgressFacility:
    """Owns the active session and drives the widget. Most-recent wins."""

    def __init__(self, app: App[Any]) -> None:
        self._app = app
        self._active: ProgressSession | None = None

    @property
    def active(self) -> ProgressSession | None:
        return self._active

    def open(self, phase: str = "", *, total: int = 1) -> ProgressSession:
        prior = self._active
        session = ProgressSession(self, phase=phase, total=total)
        self._active = session
        # Silent takeover — prior session is replaced but the widget stays visible.
        if prior is not None and not prior.closed:
            prior._closed = True
        self._render(session)
        return session

    def _widget(self) -> FNDProgressBar | None:
        with contextlib.suppress(Exception):
            return self._app.query_one(FNDProgressBar)
        return None

    def _render(self, session: ProgressSession) -> None:
        w = self._widget()
        if w is None:
            return
        w.set_phase(session.phase)
        w.set_total(session.total)
        w.set_progress(session.progress)
        w.show()

    def _on_session_update(self, session: ProgressSession) -> None:
        if session is self._active:
            self._render(session)

    def _on_session_close(self, session: ProgressSession) -> None:
        if session is not self._active:
            return
        self._active = None
        w = self._widget()
        if w is None:
            return
        w.hide()
        w.reset()
