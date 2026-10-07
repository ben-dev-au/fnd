"""DetailStrip shows a ``str`` description literally; a styled one arrives as
Content, so a toggle can colour its effect lines (green + / red -)."""

from __future__ import annotations

import pytest

from fnd.display_text import display_block
from fnd.tui.ui_text import ui_text
from fnd.tui.widgets.detail_strip import DetailStrip
from tests import _hostile_text


def test_a_styled_description_keeps_its_colours() -> None:
    strip = DetailStrip()
    strip.set(ui_text("[green]+[/] gain [red]-[/] loss"))
    desc, _meta = strip._render_lines()
    assert desc.plain == "+ gain - loss"
    assert any("green" in str(span.style) for span in desc.spans)


def test_brackets_are_literal_by_default() -> None:
    strip = DetailStrip()
    strip.set("matches **/*.[ch] and [red]not styled[/]")
    desc, _meta = strip._render_lines()
    assert desc.plain == "matches **/*.[ch] and [red]not styled[/]"
    assert not desc.spans


@pytest.mark.parametrize("raw", _hostile_text.ALL)
def test_any_text_shows_literally(raw: str) -> None:
    strip = DetailStrip()
    strip.set(raw, raw)
    desc, meta = strip._render_lines()
    assert desc.plain == display_block(raw)
    assert meta.plain == display_block(raw)
