"""At 62 columns the footer elided `Tab  Completed` and kept `↑↓ Choose`,
`⏎ Select` and `Esc Close`.

Those three are what a user tries unprompted. `Tab` was the only non-obvious
one, and the only route into the run history: the feature was advertised at
wide widths and unreachable in practice at the persona's.
"""

from __future__ import annotations

from fnd.tui.app import render_hint_bar

_HINTS = (("↑↓", "Choose"), ("⏎", "Select"), ("Tab", "Completed"), ("Esc", "Close"))


def test_an_app_specific_key_outlives_a_guessable_one() -> None:
    """The hint worth its cells is the one nobody would guess."""
    narrow = render_hint_bar((), _HINTS).fitted(44).plain

    assert "Tab" in narrow, narrow
    assert "Choose" not in narrow, narrow


def test_the_way_out_is_never_dropped() -> None:
    """The standing rule holds: leaving beats everything."""
    tiny = render_hint_bar((), _HINTS).fitted(20).plain

    assert "Esc" in tiny, tiny


def test_the_survivors_keep_bar_order() -> None:
    """Ranking decides what is KEPT, never what order it reads in.

    Width 66 is where the two differ: three keys survive and one of them is
    guessable, so a bar built in ranked order would read
    `Tab │ y │ ↑↓` instead of `↑↓ │ Tab │ y`. A width that fits everything
    never reaches the ranking at all and cannot see a reshuffle.
    """
    hints = (
        ("↑↓", "Choose"),
        ("⏎", "Select"),
        ("Tab", "Completed"),
        ("y", "Copy"),
        ("Esc", "Close"),
    )
    bar = render_hint_bar((), hints).fitted(66).plain

    assert "…" in bar, bar
    assert bar.index("Choose") < bar.index("Completed") < bar.index("Copy"), bar
