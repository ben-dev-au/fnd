"""The preview's bottom border: a collection name on the left, status on the right.

Textual gives a border one subtitle with one alignment, so when both ends are
in use they are drawn as a single label spanning the edge, the gap filled with
the border's own glyph. Unstyled label text paints in the border colour, so the
fill matches the edge through focus changes. The status is never shortened; the
name gives way first, then goes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from textual.content import Content

from fnd.display_text import display_line

if TYPE_CHECKING:
    from fnd.tui.collection_marks import Mark

#: Corners, the pad Textual puts either side of a label, and the one border
#: cell it keeps beside an aligned label: six cells of the edge are never label.
EDGE_RESERVED = 6

#: A name cut shorter than this reads as a typo, not a collection.
MIN_NAME_CELLS = 4

#: Around the fill: a space, at least one border glyph, a space; and after the
#: status the space its status-only form ends with, so the status never shifts.
_GAP = 4

#: The bottom edge of the preview's `round` border.
EDGE_GLYPH = "─"


def marked_name(name: str, mark: Mark | None) -> Content:
    """A collection's name in its colour, its shape first so a shortened name keeps it."""
    name = display_line(name)
    if mark is None:
        return Content(name)
    text = f"{mark.shape} {name}" if mark.shape else name
    return Content.styled(text, mark.colour)


def bottom_edge(
    edge_width: int, name: Content, status: Content
) -> tuple[Content, Literal["left", "right"]]:
    """The subtitle and its alignment for ``name`` and ``status`` on one edge."""
    room = edge_width - EDGE_RESERVED
    if not status.cell_length:
        if name.cell_length and room >= MIN_NAME_CELLS:
            return name.truncate(room, ellipsis=True), "left"
        return Content(""), "left"
    name_room = room - status.cell_length - _GAP
    if not name.cell_length or name_room < MIN_NAME_CELLS:
        return Content.assemble(" ", status, " "), "right"
    name = name.truncate(name_room, ellipsis=True)
    fill = room - name.cell_length - status.cell_length - (_GAP - 1)
    return Content.assemble(name, " ", EDGE_GLYPH * fill, " ", status, " "), "left"
