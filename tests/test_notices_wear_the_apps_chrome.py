"""Notices are styled like the app, and sit where they do not cover it.

Textual's stock toast is a filled `$panel-lighten-1` slab with a thick `outer`
bar down one side, padded `1 1` at a fixed 60 columns, docked bottom right.
Every pane in fnd is a thin round outline on `$surface`, so a notice read as
another program's widget, and the bottom rows carry the hint bar and the
progress strip, which it covered.

Toasts do not mount under `run_test`, so this asserts the rules; the rendering
was verified in a real terminal, both severities.
"""

from __future__ import annotations

import re

from fnd.tui import FNDApp

_CSS = FNDApp.CSS


def _block(selector: str) -> str:
    """The declarations of one rule in the app's stylesheet."""
    match = re.search(rf"(?m)^\s*{re.escape(selector)}\s*\{{(.*?)\}}", _CSS, re.S)
    assert match, f"no rule for {selector!r}"
    return " ".join(match.group(1).split())


def test_the_rack_sits_top_right() -> None:
    """The bottom rows are the hint bar and the progress strip."""
    rack = _block("ToastRack")
    assert "dock: top" in rack, rack
    assert "align: right top" in rack, rack


def test_a_notice_wears_a_round_border_like_every_pane() -> None:
    toast = _block("Toast")
    assert "border: round" in toast, toast
    assert "background: $surface" in toast, toast
    assert "width: auto" in toast, "a fixed width made short notices a slab"
    assert "padding: 0 1" in toast, toast


def test_severity_is_the_border_colour_the_app_already_uses() -> None:
    """`round $error` is exactly what the confirm screens wear, so a notice
    about a failure looks like the dialogs about destruction."""
    assert "border: round $warning" in _block("Toast.-warning")
    assert "border: round $error" in _block("Toast.-error")
    assert "border: round $primary 50%" in _block("Toast.-information")


def test_nothing_keeps_the_stock_slab() -> None:
    """The control: the two properties that made it look foreign."""
    toast = _block("Toast")
    assert "border-left: outer" not in toast, toast
    assert "$panel-lighten-1" not in toast, toast
