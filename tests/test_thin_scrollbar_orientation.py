"""ThinScrollBarRender renders the thin line glyph for BOTH orientations.

Before the fix only vertical bars rendered thin (``│``); horizontal bars
fell through to Textual's stock renderer (a reverse-video block). Both now
use the box-drawing line weight: ``│`` vertical, ``─`` horizontal.
"""

from __future__ import annotations

from rich.console import Console

from fnd.tui.preview_scrollbar import (
    _THUMB_GLYPH,
    _THUMB_GLYPH_H,
    ThinScrollBarRender,
)


def _rendered_glyphs(*, vertical: bool) -> str:
    # window < virtual so a thumb is drawn (not an all-blank track).
    render = ThinScrollBarRender(
        virtual_size=100,
        window_size=20,
        position=10,
        vertical=vertical,
        style="bright_magenta",
    )
    console = Console()
    options = (
        console.options.update(height=10)
        if vertical
        else console.options.update(width=10)
    )
    text = ""
    for segments in render.__rich_console__(console, options):
        for seg in segments.segments:
            text += seg.text
    return text


def test_vertical_thumb_uses_line_glyph() -> None:
    glyphs = _rendered_glyphs(vertical=True)
    assert _THUMB_GLYPH in glyphs
    assert "█" not in glyphs  # not the stock full-cell block


def test_horizontal_thumb_uses_line_glyph() -> None:
    glyphs = _rendered_glyphs(vertical=False)
    assert _THUMB_GLYPH_H in glyphs
    assert "█" not in glyphs  # was the stock block before the fix
