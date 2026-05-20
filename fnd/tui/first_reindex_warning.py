"""One-time warning shown before the first big reindex after the user
enables ``pdf-structure``.

The dialog appears once per state-transition where the pdf-structure
extra becomes installed. After dismissal (Continue or Cancel), a marker
file is written to ``$XDG_DATA_HOME/fnd/`` so we don't nag again.

Estimation: count PDFs in the configured collection's sources × an
average ~30s/PDF heuristic. Rough on purpose — the user just needs an
order-of-magnitude warning before walking away from a multi-hour run.
"""

from __future__ import annotations

import contextlib
from pathlib import Path

from platformdirs import user_data_dir
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static

from fnd.config import CollectionConfig
from fnd.walk import walk_sources

# Average per-PDF extraction cost when the cache is cold and the user
# has the pdf-structure extra installed. Used for the warning ETA only;
# real progress overrides this once the indexer starts.
_AVG_SECONDS_PER_PDF = 30.0


def _marker_path() -> Path:
    return Path(user_data_dir("fnd")) / "first_reindex_warning_seen"


def has_been_seen() -> bool:
    return _marker_path().exists()


def mark_seen() -> None:
    p = _marker_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        p.touch()


def reset_seen() -> None:
    """Test helper / `fnd config set indexer.show_first_reindex_warning=true`."""
    with contextlib.suppress(OSError):
        _marker_path().unlink()


def count_pdfs(config: CollectionConfig) -> int:
    n = 0
    for source in config.sources:
        for path in walk_sources(sources=[source]):
            if path.suffix.lower() == ".pdf":
                n += 1
    return n


def estimate_eta_seconds(n_pdfs: int) -> float:
    return float(n_pdfs) * _AVG_SECONDS_PER_PDF


def fmt_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"~{int(seconds / 60)} min"
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    return f"~{h}h {m}m"


class FirstReindexWarningScreen(ModalScreen[bool]):
    """Modal that warns the user about indexing time before the first
    big reindex with structured PDF extraction enabled.

    Result via ``dismiss(True)``: user wants to proceed.
    Result via ``dismiss(False)``: user cancelled.
    """

    BINDINGS = [  # noqa: RUF012
        Binding("enter", "start", "Start", show=True),
        Binding("escape", "cancel", "Cancel", show=True),
        Binding("d", "dont_show_again", "Don't show again", show=True),
    ]

    CSS = """
    FirstReindexWarningScreen { align: center middle; background: $surface 75%; }
    #first_reindex_box {
        width: 78; height: auto;
        border: round $warning;
        padding: 1 2; background: $surface;
    }
    #first_reindex_box Static { padding: 0 0 1 0; }
    #first_reindex_buttons { height: 3; padding-top: 1; }
    #first_reindex_buttons Button { margin-right: 2; }
    """

    def __init__(self, *, collection: str, n_pdfs: int) -> None:
        super().__init__()
        self._collection = collection
        self._n_pdfs = n_pdfs

    def compose(self) -> ComposeResult:
        eta_s = estimate_eta_seconds(self._n_pdfs)
        with Vertical(id="first_reindex_box"):
            yield Static(
                "[bold yellow]First reindex with structured PDF support[/]",
                id="first_reindex_title",
            )
            yield Static(
                f"This will extract structure from [bold]{self._n_pdfs} PDFs[/] in "
                f"'{self._collection}'."
            )
            yield Static(
                f"Estimated time: [bold]{fmt_duration(eta_s)}[/] "
                "(~30s per PDF on average; figure-heavy PDFs are slower)."
            )
            yield Static(
                "After this one-time cost, future reindexes only process files\n"
                "that have changed since last run."
            )
            yield Static(
                "Indexing runs in the background — you can keep searching while\n"
                "it works. fnd will auto-resume on next launch if you quit."
            )
            with Horizontal(id="first_reindex_buttons"):
                yield Button("Start", id="first_reindex_start", variant="primary")
                yield Button("Cancel", id="first_reindex_cancel", variant="warning")
                yield Button("Don't show again", id="first_reindex_dont_show")

    def action_start(self) -> None:
        mark_seen()
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)

    def action_dont_show_again(self) -> None:
        mark_seen()
        self.dismiss(True)

    def on_button_pressed(self, ev: Button.Pressed) -> None:
        if ev.button.id == "first_reindex_start":
            self.action_start()
        elif ev.button.id == "first_reindex_cancel":
            self.action_cancel()
        elif ev.button.id == "first_reindex_dont_show":
            self.action_dont_show_again()


__all__ = [
    "FirstReindexWarningScreen",
    "count_pdfs",
    "estimate_eta_seconds",
    "fmt_duration",
    "has_been_seen",
    "mark_seen",
    "reset_seen",
]
