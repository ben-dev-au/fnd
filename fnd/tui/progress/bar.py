"""The app-level progress strip widget.

One row at the bottom of the layout, below the panes and above the
footer hints. Hidden via ``visibility: hidden`` rather than ``display``
so the row occupancy is constant and toggling never reflows the panes.
"""

from __future__ import annotations

import contextlib

from textual.containers import Horizontal
from textual.widget import Widget
from textual.widgets import Label, ProgressBar


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


__all__ = ["FNDProgressBar"]
