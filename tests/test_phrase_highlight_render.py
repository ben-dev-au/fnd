"""Render-layer highlighting is phrase-aware for quoted queries."""

from __future__ import annotations

from rich.text import Text

from fnd.matching import MatchSpec
from fnd.render import apply_match_highlights, text_has_any_match


def test_text_has_any_match_true_for_phrase_line() -> None:
    spec = MatchSpec.from_query('"defence in depth"', auto_fuzzy=False)
    assert text_has_any_match("Our defence in depth plan", spec)


def test_text_has_any_match_false_for_lone_phrase_word() -> None:
    spec = MatchSpec.from_query('"defence in depth"', auto_fuzzy=False)
    # Only one phrase word present, no contiguous phrase → not a match.
    assert not text_has_any_match("we value depth here", spec)


def test_apply_highlights_covers_phrase_span() -> None:
    spec = MatchSpec.from_query('"defence in depth"', auto_fuzzy=False)
    line = "Our defence in depth plan"
    t = Text(line)
    found = apply_match_highlights(t, spec)
    assert found
    p_start = line.index("defence")
    p_end = line.index("depth") + len("depth")
    covered = [c for c in range(p_start, p_end) if any(s.start <= c < s.end for s in t.spans)]
    # Every char of the phrase is inside some highlight span.
    assert len(covered) == p_end - p_start
    # The stopword "in" outside is only covered because it's inside the phrase;
    # a stray "in" elsewhere must not be highlighted.
    line2 = "stay in the loop"
    t2 = Text(line2)
    assert not apply_match_highlights(t2, spec)
