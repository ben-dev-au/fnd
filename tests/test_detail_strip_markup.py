"""DetailStrip renders a row's description as Rich markup only when the row
opts in (``markup=True``) so toggles can colour their effect lines (green +
/ red -). By default — and for arbitrary text like paths/globs/notes —
brackets render literally. Malformed opted-in markup falls back to literal
text rather than raising."""

from __future__ import annotations

from fnd.tui.widgets.detail_strip import DetailStrip


def test_markup_is_parsed_and_coloured_when_opted_in() -> None:
    strip = DetailStrip()
    strip.set("[green]+[/] gain [red]-[/] loss", markup=True)
    desc, _meta = strip._render_lines()
    # Markup tags are consumed, not shown literally.
    assert desc.plain == "+ gain - loss"
    assert "[green]" not in desc.plain
    # At least one coloured span survives.
    assert any("green" in str(span.style) for span in desc.spans)


def test_brackets_are_literal_by_default() -> None:
    # Default (no opt-in): a description containing tag-like text — e.g. a
    # glob char-class or an app note — must render verbatim.
    strip = DetailStrip()
    strip.set("matches **/*.[ch] and [red]not styled[/]")
    desc, _meta = strip._render_lines()
    assert desc.plain == "matches **/*.[ch] and [red]not styled[/]"
    assert not desc.spans  # no markup applied


def test_malformed_markup_falls_back_to_literal() -> None:
    strip = DetailStrip()
    strip.set("[/] dangling close", markup=True)
    desc, _meta = strip._render_lines()
    assert "dangling close" in desc.plain
