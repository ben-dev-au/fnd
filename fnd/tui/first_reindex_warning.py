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
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Static

from fnd.config import CollectionConfig
from fnd.walk import walk_sources

# Average per-PDF extraction cost when the cache is cold and the user
# has the pdf-structure extra installed. Used for the warning ETA only;
# real progress overrides this once the indexer starts.
# Removed in favour of :mod:`fnd.tui.cost_estimate` so the figure
# calibrates to the user's machine after their first Update index
# run, rather than every cost estimate in the app being a hard-coded
# constant. ``estimate_seconds_for(n_pdfs)`` is the single source.


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
    """Calibrated ETA. Falls back to the bake-off constant via
    :mod:`fnd.tui.cost_estimate` when no run history exists yet."""
    from fnd.tui.cost_estimate import estimate_seconds_for

    return estimate_seconds_for(n_pdfs)


def fmt_duration(seconds: float) -> str:
    """Shim. The new canonical formatter lives in
    :mod:`fnd.tui.cost_estimate` so disclosures use the same shape
    across screens."""
    from fnd.tui.cost_estimate import format_duration

    return format_duration(seconds)


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
        width: 78;
        height: auto;
        min-height: 12;
        border: round $warning;
        padding: 1 2;
        background: $surface;
    }
    #first_reindex_box > Static { height: auto; padding: 0 0 1 0; }
    #first_reindex_body { padding: 0 0 1 0; }
    #first_reindex_buttons {
        height: 1;
        width: 100%;
        margin: 1 0 0 0;
        color: $accent;
        text-style: bold;
    }
    """

    def __init__(self, *, collection: str, n_pdfs: int) -> None:
        super().__init__()
        self._collection = collection
        self._n_pdfs = n_pdfs

    def compose(self) -> ComposeResult:
        from rich.text import Text

        from fnd.tui.settings_screen import build_confirm_body

        eta_s = estimate_eta_seconds(self._n_pdfs)
        body = build_confirm_body(
            outcome_label="What",
            outcome=(
                f"First reindex of '{self._collection}'. Extracts structure from "
                f"{self._n_pdfs} PDFs (headings, lists, tables)."
            ),
            cost_label="Time",
            cost=f"~{fmt_duration(eta_s)} on cold start. Future reindexes only touch changed files.",
            safety_label="Background",
            safety="Runs in the background. Keep searching while it works. Auto-resumes if you quit.",
        )
        with Vertical(id="first_reindex_box"):
            yield Static(
                Text("First reindex with structured PDF support", style="bold yellow"),
                id="first_reindex_title",
                markup=False,
            )
            yield Static(body, id="first_reindex_body")
            yield Static(
                "[ Start ]   [ Cancel ]   [ Don't show again ]",
                id="first_reindex_buttons",
                markup=False,
            )

    def action_start(self) -> None:
        mark_seen()
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)

    def action_dont_show_again(self) -> None:
        mark_seen()
        self.dismiss(True)


__all__ = [
    "FirstReindexWarningScreen",
    "count_pdfs",
    "estimate_eta_seconds",
    "fmt_duration",
    "has_been_seen",
    "mark_seen",
    "reset_seen",
]
