"""DetailStrip renders a row's description as Rich markup so settings
toggles can colour the effect lines (e.g. green + / red -). Malformed
markup falls back to literal text rather than raising."""

from __future__ import annotations

from fnd.tui.widgets.detail_strip import DetailStrip


def test_description_markup_is_parsed_and_coloured() -> None:
    strip = DetailStrip()
    strip.set("[green]+[/] gain [red]-[/] loss")
    desc, _meta = strip._render_lines()
    # Markup tags are consumed, not shown literally.
    assert desc.plain == "+ gain - loss"
    assert "[green]" not in desc.plain
    # At least one coloured span survives.
    assert any("green" in str(span.style) for span in desc.spans)


def test_malformed_markup_falls_back_to_literal() -> None:
    strip = DetailStrip()
    strip.set("[/] dangling close")
    desc, _meta = strip._render_lines()
    assert "dangling close" in desc.plain
