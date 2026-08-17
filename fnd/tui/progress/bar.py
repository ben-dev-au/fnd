"""The progress line — one row spanning the full width of the panes.

Blank at rest. The row itself is always reserved (``visibility: hidden``,
not ``display: none``) so showing it never reflows the panes above, but
nothing is painted until an operation starts.

Drawn as one box-drawing glyph per cell in the pane border's weight,
following :class:`fnd.tui.preview_scrollbar.ThinScrollBarRender` — the
filled run in the accent, the remainder as a dim rule — so the line
reads as part of the frame rather than as a widget bolted underneath it.
Integer cells only; no partial-block end caps, which flicker.
"""

from __future__ import annotations

from typing import ClassVar

from rich.cells import cell_len
from rich.console import Group
from rich.segment import Segment
from rich.style import Style as RichStyle
from rich.text import Text
from textual.app import RenderResult
from textual.reactive import reactive
from textual.widget import Widget

# Both runs are the pane border's own rule, so the line sits in the frame at
# exactly the weight of the borders it sits under; only colour separates the
# filled part from the remainder. A heavier glyph for the fill was tried first
# and read as too loud — progress is ambient information, not an alert, and it
# should not compete with the content for the eye. If it needs to recede
# further, dim the accent rather than changing the glyph again.
FILL_GLYPH = "─"
TRACK_GLYPH = "─"

# Below this the bar carries no information, so the label is dropped and
# the whole row goes to the bar.
_MIN_BAR_CELLS = 12
# Gap between the bar and its label.
_LABEL_GAP = 2


def progress_line_segments(
    *,
    width: int,
    fraction: float,
    label: str = "",
    fill_style: RichStyle | None = None,
    track_style: RichStyle | None = None,
    label_style: RichStyle | None = None,
) -> list[Segment]:
    """Render one progress row. Pure — no widget, no console, no styles
    resolution — so the layout rules below are unit-testable directly."""
    if width <= 0:
        return []
    fraction = 0.0 if fraction < 0.0 else (1.0 if fraction > 1.0 else fraction)

    bar_width = width
    shown_label = ""
    if label:
        cost = cell_len(label) + _LABEL_GAP
        if width - cost >= _MIN_BAR_CELLS:
            shown_label = label
            bar_width = width - cost

    filled = _filled_cells(bar_width, fraction)
    segments = [
        Segment(FILL_GLYPH * filled, fill_style),
        Segment(TRACK_GLYPH * (bar_width - filled), track_style),
    ]
    if shown_label:
        segments.append(Segment(" " * _LABEL_GAP, track_style))
        segments.append(Segment(shown_label, label_style))
    return [s for s in segments if s.text]


def _filled_cells(bar_width: int, fraction: float) -> int:
    """Cells to paint as filled.

    Two roundings that matter more than they look: any progress at all
    shows at least one cell (a bar that reads as empty at 3% is the
    "nothing is happening" complaint), and a bar short of completion
    always leaves at least one cell unfilled (so a full line means done
    and nothing else does).
    """
    if bar_width <= 0:
        return 0
    if fraction >= 1.0:
        return bar_width
    filled = round(bar_width * fraction)
    if fraction > 0.0:
        filled = max(1, filled)
    return min(filled, bar_width - 1)


class FNDProgressBar(Widget):
    """Widget wrapper. All layout logic lives in the pure function above."""

    COMPONENT_CLASSES: ClassVar[set[str]] = {
        "progress--fill",
        "progress--fill-ambient",
        "progress--track",
        "progress--label",
    }

    DEFAULT_CSS = """
    FNDProgressBar {
        height: 1;
        width: 100%;
        padding: 0 1;
        background: transparent;
    }
    /* A second row, and only while background work is actually running. It
       carries that work's status text, which is why the bar's own row never
       has to: `─` is drawn at the middle of its cell but text sits on a
       baseline near the bottom of one, so a label sharing the bar's row reads
       as crowding the footer with a gap above it. That is the font, not the
       layout, and the only fix is to keep text out of that row. */
    FNDProgressBar.-with-status { height: 2; }
    /* visibility:hidden keeps the row, so toggling never reflows the panes above. */
    FNDProgressBar.-idle { visibility: hidden; }
    FNDProgressBar > .progress--fill  { color: $accent; }
    /* Ambient work — a background reindex — in the same accent at half
       strength. With the status row, the two can now be on screen at once:
       the bar is whatever the user is waiting on, the row beneath it is what
       is running on its own. The dimmer fill is what says which is which when
       the background run has the bar to itself. */
    FNDProgressBar > .progress--fill-ambient { color: $accent 50%; }
    /* Same colour family and weight as ``border: round $primary 50%`` on the
       panes, one step dimmer so the fill reads against it. */
    FNDProgressBar > .progress--track { color: $primary 30%; }
    FNDProgressBar > .progress--label { color: $text-muted; }
    """

    fraction: reactive[float] = reactive(0.0)
    label: reactive[str] = reactive("")
    ambient: reactive[bool] = reactive(False)
    status: reactive[str] = reactive("")

    def __init__(self) -> None:
        super().__init__(id="fnd_progress", classes="-idle")

    @property
    def is_idle(self) -> bool:
        return self.has_class("-idle")

    def show(self) -> None:
        self.remove_class("-idle")

    def hide(self) -> None:
        self.add_class("-idle")

    def reset(self) -> None:
        self.fraction = 0.0
        self.label = ""
        self.ambient = False
        self.status = ""

    def watch_status(self, status: str) -> None:
        """The status row exists only while there is something to put in it.

        Reserving it permanently would cost a row of preview for something
        that is idle almost all the time; taking the row for the duration of
        a background run costs one reflow at each end of a run that lasts
        minutes. A height change does not re-wrap the preview — only a width
        change does — so this is a shift, not a reflow of the content.
        """
        self.set_class(bool(status), "-with-status")

    def render(self) -> RenderResult:
        fill = "progress--fill-ambient" if self.ambient else "progress--fill"
        bar = Text(no_wrap=True, overflow="crop")
        for segment in progress_line_segments(
            width=self.content_size.width,
            fraction=self.fraction,
            label=self.label,
            fill_style=self.get_component_rich_style(fill),
            track_style=self.get_component_rich_style("progress--track"),
            label_style=self.get_component_rich_style("progress--label"),
        ):
            bar.append(segment.text, segment.style)
        if not self.status:
            return bar
        return Group(
            bar,
            Text(
                self.status,
                style=self.get_component_rich_style("progress--label"),
                no_wrap=True,
                overflow="ellipsis",
                end="",
            ),
        )


__all__ = ["FILL_GLYPH", "TRACK_GLYPH", "FNDProgressBar", "progress_line_segments"]
