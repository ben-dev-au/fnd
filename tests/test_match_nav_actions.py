"""n / b are bound to the match-navigation actions."""

from __future__ import annotations

from fnd.tui.actions import load_keymap


def test_n_and_b_are_bound() -> None:
    b = load_keymap().bindings
    assert b.get("n") == "nav_next_match"
    assert b.get("b") == "nav_prev_match"
