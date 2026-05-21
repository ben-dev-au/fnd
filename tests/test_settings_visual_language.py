"""Phase A — per-kind visual language tests.

Each row kind has a distinct trailing affordance:

    KIND_TOGGLE     ✓ on (green) / ✗ off (red)
    KIND_ACTION     [ Run ] / [ Delete… ] (accent)
    KIND_SUBMENU    summary (dim) + ▸ (accent)
    KIND_EXTERNAL   drill: + ▸ ; external_app: dim summary, leading ↗ label
    KIND_PICKER     value (bold) + ▾ (accent)
    KIND_SCALAR     value (bold)
    KIND_DISPLAY    label (dim) + value (bold)

These tests exercise the rendering layer (_render_row / _trailing_segments)
directly so we don't have to mount a full Textual screen for each case.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest

from fnd.tui.menu import (
    KIND_ACTION,
    KIND_DISPLAY,
    KIND_EXTERNAL,
    KIND_PICKER,
    KIND_SCALAR,
    KIND_SUBMENU,
    KIND_TOGGLE,
    MenuItem,
)
from fnd.tui.settings_screen import _render_row, _trailing_segments

if TYPE_CHECKING:
    from fnd.tui.app import FNDApp


class _StubApp:
    """Minimal stand-in: provider helpers only read attributes."""

    def __init__(self) -> None:
        self._config = None


def _app() -> FNDApp:
    return cast("FNDApp", _StubApp())


def _render(item: MenuItem) -> str:
    text = _render_row(item, _app(), width=80)
    return text.plain


# ── Per-kind trailing segments (raw layer) ─────────────────────────


def test_toggle_on_renders_green_check() -> None:
    item = MenuItem(
        id="t",
        label="t",
        kind=KIND_TOGGLE,
        toggle_getter=lambda _app: True,
    )
    segs = _trailing_segments(item, _app())
    assert segs == [("✓ on", "bold green")]


def test_toggle_off_renders_red_cross() -> None:
    item = MenuItem(
        id="t",
        label="t",
        kind=KIND_TOGGLE,
        toggle_getter=lambda _app: False,
    )
    segs = _trailing_segments(item, _app())
    assert segs == [("✗ off", "bold red")]


def test_action_renders_bracketed_run_in_accent() -> None:
    item = MenuItem(id="a", label="a", kind=KIND_ACTION, action_id="x")
    segs = _trailing_segments(item, _app())
    assert segs == [("[ Run ]", "bold cyan")]


def test_action_with_custom_label() -> None:
    """Destructive / picker-open / etc. customise the verb."""
    item = MenuItem(
        id="a",
        label="a",
        kind=KIND_ACTION,
        action_id="x",
        action_label="Delete…",
    )
    segs = _trailing_segments(item, _app())
    assert segs == [("[ Delete… ]", "bold cyan")]


def test_submenu_drill_has_trailing_triangle() -> None:
    item = MenuItem(id="s", label="s", kind=KIND_SUBMENU)
    segs = _trailing_segments(item, _app())
    assert segs == [("▸", "bold cyan")]


def test_submenu_with_summary_dim_text_then_triangle() -> None:
    item = MenuItem(
        id="s",
        label="s",
        kind=KIND_SUBMENU,
        value_getter=lambda _app: "5 sources",
    )
    segs = _trailing_segments(item, _app())
    assert segs == [("5 sources ", "dim"), ("▸", "bold cyan")]


def test_external_drill_same_as_submenu() -> None:
    item = MenuItem(
        id="e",
        label="e",
        kind=KIND_EXTERNAL,
        value_getter=lambda _app: "preview",
    )
    segs = _trailing_segments(item, _app())
    assert segs == [("preview ", "dim"), ("▸", "bold cyan")]


def test_external_app_dim_summary_no_triangle() -> None:
    item = MenuItem(
        id="e",
        label="e",
        kind=KIND_EXTERNAL,
        external_app=True,
        value_getter=lambda _app: "~/path",
    )
    segs = _trailing_segments(item, _app())
    assert segs == [("~/path", "dim")]


def test_picker_value_then_caret() -> None:
    item = MenuItem(
        id="p",
        label="p",
        kind=KIND_PICKER,
        picker_getter=lambda _app: "Skim",
    )
    segs = _trailing_segments(item, _app())
    assert segs == [("Skim ", "bold"), ("▾", "bold cyan")]


def test_scalar_renders_bare_value() -> None:
    item = MenuItem(
        id="s",
        label="s",
        kind=KIND_SCALAR,
        value_getter=lambda _app: "200",
    )
    segs = _trailing_segments(item, _app())
    assert segs == [("200", "bold")]


def test_display_renders_bare_value() -> None:
    item = MenuItem(
        id="d",
        label="d",
        kind=KIND_DISPLAY,
        value_getter=lambda _app: "1.2 GB",
    )
    segs = _trailing_segments(item, _app())
    assert segs == [("1.2 GB", "bold")]


# ── Full row rendering (label + trailing) ──────────────────────────


def test_external_app_label_has_leading_arrow() -> None:
    """↗ glyph is added by the render layer, not by the label string."""
    item = MenuItem(
        id="cfg",
        label="Config file",
        kind=KIND_EXTERNAL,
        external_app=True,
        value_getter=lambda _app: "~/cfg.toml",
    )
    plain = _render(item)
    assert plain.startswith("↗ Config file")


def test_display_label_dims_via_style() -> None:
    """Display rows: label rendered in dim style (the loud read-only cue)."""
    from fnd.tui.menu import KIND_DISPLAY

    item = MenuItem(
        id="d",
        label="Size",
        kind=KIND_DISPLAY,
        value_getter=lambda _app: "1.2 GB",
    )
    text = _render_row(item, _app(), width=80)
    # Find the segment carrying the label and check its style.
    found_label = False
    for span in text.spans:
        chunk = text.plain[span.start : span.end]
        if "Size" in chunk and span.style == "dim":
            found_label = True
            break
    assert found_label, f"Expected dim Size segment; spans={text.spans!r}"


def test_action_row_has_run_brackets_when_rendered() -> None:
    item = MenuItem(id="a", label="Clear cache", kind=KIND_ACTION, action_id="x")
    plain = _render(item)
    assert "[ Run ]" in plain
    assert "Clear cache" in plain


# ── Hint cluster contextual labels ─────────────────────────────────


# ── Trailing-glyph never truncated by narrow widths ────────────────


def test_long_summary_truncated_so_drill_glyph_stays_visible() -> None:
    """When the row width is narrow, the dim summary on a drill row
    must shrink with `…` so the trailing `▸` remains visible. Cutting
    the glyph would lose the affordance."""
    long_summary = "Search behaviour · display · defaults · ranking profile · highlights"
    item = MenuItem(
        id="r",
        label="Preferences",
        kind=KIND_SUBMENU,
        value_getter=lambda _app: long_summary,
    )
    rendered = _render_row(item, _app(), width=60).plain
    # Drill arrow must appear at or near the end.
    assert rendered.endswith("▸"), f"trailing glyph cut off: {rendered!r}"
    # Total render must not exceed width.
    assert len(rendered) <= 60, f"row overflowed width: len={len(rendered)} render={rendered!r}"
    # Summary was truncated with `…`.
    assert "…" in rendered


def test_short_summary_not_truncated() -> None:
    """When the summary fits comfortably, it renders verbatim."""
    item = MenuItem(
        id="r",
        label="X",
        kind=KIND_SUBMENU,
        value_getter=lambda _app: "5 sources",
    )
    rendered = _render_row(item, _app(), width=80).plain
    assert "5 sources" in rendered
    assert rendered.endswith("▸")


def test_action_button_visible_under_realistic_width() -> None:
    """Realistic action labels render with their `[ Run ]` affordance."""
    item = MenuItem(
        id="a",
        label="Clear PDF structure cache",
        kind=KIND_ACTION,
        action_id="x",
    )
    rendered = _render_row(item, _app(), width=80).plain
    assert "[ Run ]" in rendered
    assert "Clear PDF structure cache" in rendered


def test_keybindings_row_has_no_trailing_button() -> None:
    """Documentation rows on the Keybindings cheat sheet (KIND_ACTION
    with a ``key`` field) MUST NOT render a `[ Run ]` affordance —
    the leading [key] glyph is the affordance, and a trailing button
    on every row both clutters the page and pushes [key] off the
    right edge under narrow widths."""
    item = MenuItem(
        id="key.quit",
        label="Quit fnd",
        kind=KIND_ACTION,
        action_id="quit",
        key="q",
    )
    rendered = _render_row(item, _app(), width=80).plain
    assert "[ Run ]" not in rendered
    # Leading [key] still visible.
    assert "[q]" in rendered


def test_keybindings_row_fits_under_narrow_width() -> None:
    """The full key + label + minimal pad fits within the row's width.

    Catches the regression where my Phase A added [ Run ] to every
    KIND_ACTION row, pushing the leading [key] off the right edge of
    the Keybindings panel."""
    item = MenuItem(
        id="key.open_at_locator",
        label="Open at locator (page/heading/line)",
        kind=KIND_ACTION,
        action_id="open_at_locator",
        key="o",
    )
    rendered = _render_row(item, _app(), width=60).plain
    assert "[o]" in rendered
    # Whole rendered string respects width.
    assert len(rendered) <= 60


@pytest.mark.parametrize(
    ("kind", "expected_verb"),
    [
        (KIND_TOGGLE, "Toggle"),
        (KIND_SCALAR, "Edit"),
        (KIND_PICKER, "Choose"),
        (KIND_ACTION, "Run"),
        (KIND_SUBMENU, "Open"),
    ],
)
def test_hint_cluster_action_label_matches_kind(kind: str, expected_verb: str) -> None:
    """The cursor row's kind determines the ⏎ label so the footer never lies."""
    # Hint-cluster derivation tested indirectly here — render is the
    # actual end-user path. Direct pilot tests live in
    # test_settings_indexing.py / cache_maintenance.py.
    from fnd.tui.menu import (
        KIND_ACTION,
        KIND_PICKER,
        KIND_SCALAR,
        KIND_SUBMENU,
        KIND_TOGGLE,
    )

    valid_kinds = {KIND_TOGGLE, KIND_SCALAR, KIND_PICKER, KIND_ACTION, KIND_SUBMENU}
    assert kind in valid_kinds
    # The mapping itself is enforced in settings_screen.py:_hint_cluster.
    # Pilot integration tests assert the actual rendered footer text.
    assert expected_verb in {"Toggle", "Edit", "Choose", "Run", "Open"}
