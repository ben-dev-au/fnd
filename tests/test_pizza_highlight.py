"""Bug A: query 'pizza' on doc 'pizzas' should paint 'pizza' yellow and 's' orange."""

from __future__ import annotations

from fnd.matching import MatchSpec
from fnd.render import HIGHLIGHT_STYLE, MISMATCH_STYLE, word_highlight_runs


def test_pizza_pizzas_splits_match_and_extra_char() -> None:
    spec = MatchSpec.from_query("pizza", auto_fuzzy=True)
    runs = word_highlight_runs("pizzas", spec)
    # Expect two runs: the first 5 chars yellow, the last char orange.
    assert runs == [
        (0, 5, HIGHLIGHT_STYLE),
        (5, 6, MISMATCH_STYLE),
    ], f"runs were {runs}"


def test_exact_match_is_all_yellow() -> None:
    spec = MatchSpec.from_query("pizza", auto_fuzzy=True)
    runs = word_highlight_runs("pizza", spec)
    assert runs == [(0, 5, HIGHLIGHT_STYLE)], f"runs were {runs}"


def test_case_difference_alone_is_all_yellow() -> None:
    spec = MatchSpec.from_query("pizza", auto_fuzzy=True)
    runs = word_highlight_runs("Pizza", spec)
    assert runs == [(0, 5, HIGHLIGHT_STYLE)], f"runs were {runs}"


def test_substitution_paints_substituted_char_orange() -> None:
    """Query 'pizza' on doc 'pizzs' — last 'a' replaced with 's'."""
    spec = MatchSpec.from_query("pizza", auto_fuzzy=True)
    # Aligning "pizzs" vs "pizza":
    # p-p, i-i, z-z, z-z, then 's' vs 'a' (substitution) — orange.
    runs = word_highlight_runs("pizzs", spec)
    assert runs == [
        (0, 4, HIGHLIGHT_STYLE),
        (4, 5, MISMATCH_STYLE),
    ], f"runs were {runs}"
