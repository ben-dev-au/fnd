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
from typing import Any

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Static

from fnd import paths
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
    return paths.first_reindex_marker_path()


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
    from fnd.config import load as _load_config
    from fnd.walk import resolve_skip_dirs

    try:
        skip = resolve_skip_dirs(_load_config().defaults)
    except Exception:
        skip = resolve_skip_dirs(None)
    n = 0
    for source in config.sources:
        for path in walk_sources(sources=[source], skip_dirs=skip):
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
    """One-time disclosure before the first structured-PDF run.

    Visually matches the cache confirm screens: bordered box, title,
    three-row labelled body, OptionList action rows. Keyboard: up/down
    move through the options, Enter selects.

    Result via ``dismiss(True)``: user wants to proceed.
    Result via ``dismiss(False)``: user cancelled.
    """

    BINDINGS = [  # noqa: RUF012
        Binding("escape", "cancel", "Cancel", show=False),
        Binding("up,k", "cursor(-1)", show=False),
        Binding("down,j", "cursor(1)", show=False),
        Binding("enter", "activate", show=False),
    ]

    CSS = """
    FirstReindexWarningScreen { align: center middle; background: $surface 75%; }
    #first_reindex_box {
        width: auto;
        min-width: 60;
        max-width: 100;
        height: auto;
        max-height: 90%;
        border: round $warning;
        padding: 0 1;
        background: $surface;
    }
    #first_reindex_body { padding: 0 0 1 0; }
    #first_reindex_list { height: auto; }
    """

    def __init__(self, *, collection: str, n_pdfs: int) -> None:
        super().__init__()
        self._collection = collection
        self._n_pdfs = n_pdfs

    def compose(self) -> ComposeResult:
        from rich.text import Text
        from textual.widgets import OptionList
        from textual.widgets.option_list import Option

        from fnd.tui.cost_estimate import has_calibration_data
        from fnd.tui.settings_screen import build_confirm_body

        eta_s = estimate_eta_seconds(self._n_pdfs)
        time_caveat = "" if has_calibration_data() else " (rough estimate)"
        body = build_confirm_body(
            outcome_label="What",
            outcome=(
                f"Extracts structured layout from {self._n_pdfs} PDFs in "
                f"'{self._collection}' (headings, lists, tables)."
            ),
            cost_label="Time",
            cost=f"{fmt_duration(eta_s)} on first run{time_caveat}. Future runs only touch changed files.",
            safety_label="Background",
            safety="Keep searching while it works. Auto-resumes if you quit.",
        )
        with Vertical(id="first_reindex_box") as box:
            box.border_title = "First structured-PDF run"
            yield Static(body, id="first_reindex_body")
            yield OptionList(
                Option(Text("Start", style="bold green"), id="start"),
                Option("Don't show this again, start now", id="dont_show"),
                Option("Cancel", id="cancel"),
                id="first_reindex_list",
            )

    def on_mount(self) -> None:
        from textual.widgets import OptionList

        self.query_one("#first_reindex_list", OptionList).focus()

    def action_cursor(self, direction: int) -> None:
        from textual.widgets import OptionList

        lst = self.query_one("#first_reindex_list", OptionList)
        if direction > 0:
            lst.action_cursor_down()
        else:
            lst.action_cursor_up()

    def action_activate(self) -> None:
        from textual.widgets import OptionList

        self.query_one("#first_reindex_list", OptionList).action_select()

    def action_cancel(self) -> None:
        self.dismiss(False)

    def action_start(self) -> None:
        mark_seen()
        self.dismiss(True)

    def action_dont_show_again(self) -> None:
        mark_seen()
        self.dismiss(True)

    def on_option_list_option_selected(self, ev: Any) -> None:
        if ev.option.id == "start" or ev.option.id == "dont_show":
            mark_seen()
            self.dismiss(True)
        elif ev.option.id == "cancel":
            self.dismiss(False)


__all__ = [
    "FirstReindexWarningScreen",
    "count_pdfs",
    "estimate_eta_seconds",
    "fmt_duration",
    "has_been_seen",
    "mark_seen",
    "reset_seen",
]
