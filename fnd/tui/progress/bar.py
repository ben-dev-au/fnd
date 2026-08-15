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
from rich.segment import Segment, Segments
from rich.style import Style as RichStyle
from textual.app import RenderResult
from textual.reactive import reactive
from textual.widget import Widget

# Same family as the thin scrollbar's thumb: the filled run is the heavy
# rule, the remainder the light one, so progress reads as a change in
# weight as well as colour.
FILL_GLYPH = "━"
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
    /* visibility:hidden keeps the row, so toggling never reflows the panes above. */
    FNDProgressBar.-idle { visibility: hidden; }
    FNDProgressBar > .progress--fill  { color: $accent; }
    /* Same colour family and weight as ``border: round $primary 50%`` on the
       panes, one step dimmer so the fill reads against it. */
    FNDProgressBar > .progress--track { color: $primary 30%; }
    FNDProgressBar > .progress--label { color: $text-muted; }
    """

    fraction: reactive[float] = reactive(0.0)
    label: reactive[str] = reactive("")

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

    def render(self) -> RenderResult:
        return Segments(
            progress_line_segments(
                width=self.content_size.width,
                fraction=self.fraction,
                label=self.label,
                fill_style=self.get_component_rich_style("progress--fill"),
                track_style=self.get_component_rich_style("progress--track"),
                label_style=self.get_component_rich_style("progress--label"),
            ),
            new_lines=False,
        )


__all__ = ["FILL_GLYPH", "TRACK_GLYPH", "FNDProgressBar", "progress_line_segments"]
