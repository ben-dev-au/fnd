"""GFM pipe-table serialisation, shared by the docx / odf / data / html paths.

Lifted out of the docx extractor so every table-bearing format renders tables
identically in the structural preview.
"""

from __future__ import annotations

from collections.abc import Sequence


def _escape_cell(text: str) -> str:
    # Collapse internal newlines to spaces so the pipe layout stays single-line;
    # escape pipes so they don't break the column alignment.
    return (text or "").replace("\n", " ").replace("|", r"\|").strip()


def gfm_table(rows: Sequence[Sequence[str]]) -> str:
    """Serialise ``rows`` as a GFM pipe table (first row = header).

    Returns ``""`` for an empty table or a zero-width header. Short body rows
    are padded to the header width so column counts stay consistent.
    """
    grid = [list(r) for r in rows]
    if not grid:
        return ""
    header = [_escape_cell(c) for c in grid[0]]
    width = len(header)
    if width == 0:
        return ""
    lines = [
        "| " + " | ".join(header) + " |",
        "|" + "|".join(["------"] * width) + "|",
    ]
    for row in grid[1:]:
        cells = [_escape_cell(c) for c in row][:width]
        cells += [""] * (width - len(cells))
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)
