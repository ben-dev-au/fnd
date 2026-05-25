"""Phase E — ConfirmBody helper + tri-band severity colour.

Every confirm screen renders three labelled rows (Outcome / Cost /
Safety) via :func:`build_confirm_body`. Severity controls:

  • Verb colour on the "Yes, …" option (green / yellow / red).
  • Screen-level CSS class (-safe / -recoverable / -destructive) that
    drives the bordered settings_box colour.
  • Optional "Cannot be undone" tag on irreversible bodies.
"""

from __future__ import annotations

from fnd.tui.settings_screen import (
    build_confirm_body,
    confirm_border_class,
    confirm_yes_option,
)

# ── build_confirm_body ─────────────────────────────────────────────


def test_body_has_three_labelled_rows() -> None:
    body = build_confirm_body(
        outcome="A clean outcome.",
        cost="A clean cost.",
        safety="Safe.",
    )
    text = body.plain
    assert "Outcome" in text
    assert "Cost" in text
    assert "Safety" in text
    assert "A clean outcome." in text
    assert "A clean cost." in text
    assert "Safe." in text


def test_irreversible_appends_cannot_be_undone() -> None:
    body = build_confirm_body(outcome="o", cost="c", safety="s", irreversible=True)
    assert "Cannot be undone" in body.plain
    assert "⚠" in body.plain


def test_reversible_omits_cannot_be_undone() -> None:
    body = build_confirm_body(outcome="o", cost="c", safety="s")
    assert "Cannot be undone" not in body.plain


def test_body_styles_labels_dim_values_normal() -> None:
    """Labels are rendered in $text-muted (dim), values in default
    style. Test by walking the Rich Text spans."""
    body = build_confirm_body(outcome="hello", cost="x", safety="y")
    dim_labels = [body.plain[span.start : span.end] for span in body.spans if span.style == "dim"]
    # Each label "Outcome   " / "Cost      " / "Safety    " is dim.
    assert any("Outcome" in s for s in dim_labels)
    assert any("Cost" in s for s in dim_labels)
    assert any("Safety" in s for s in dim_labels)


# ── confirm_yes_option ─────────────────────────────────────────────


def test_yes_option_green_for_safe() -> None:
    opt = confirm_yes_option("Yes, install", severity="safe")
    assert opt.id == "yes"
    prompt = opt.prompt
    assert hasattr(prompt, "plain")
    assert "install" in prompt.plain  # type: ignore[union-attr]
    # Rich Text built with `style="bold green"` stores the style on the
    # Text's own style attribute, not in a span.
    assert "green" in str(prompt.style)  # type: ignore[union-attr]


def test_yes_option_yellow_for_recoverable() -> None:
    opt = confirm_yes_option("Yes, prune", severity="recoverable")
    assert "yellow" in str(opt.prompt.style)  # type: ignore[union-attr]


def test_yes_option_red_for_destructive() -> None:
    opt = confirm_yes_option("Yes, clear", severity="destructive")
    assert "red" in str(opt.prompt.style)  # type: ignore[union-attr]


# ── confirm_border_class ───────────────────────────────────────────


def test_border_class_per_severity() -> None:
    assert confirm_border_class("safe") == "-safe"
    assert confirm_border_class("recoverable") == "-recoverable"
    assert confirm_border_class("destructive") == "-destructive"


def test_border_class_unknown_falls_back_to_safe() -> None:
    assert confirm_border_class("nonsense") == "-safe"
