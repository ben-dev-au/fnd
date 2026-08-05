"""Row snippets centre on a match the preview would actually highlight.

``_make_snippet`` used to locate its window with literal substring containment,
so a hit reached through the stemmer ("testing" for "test"), a wildcard, or a
quoted phrase found no anchor at all and fell back to the chunk's opening 240
characters — a listed row whose snippet showed no match. Measured at 26 of 993
rows on a real corpus before the change.
"""

from __future__ import annotations

from fnd.matching import MatchSpec
from fnd.query import _make_snippet
from fnd.render import text_has_any_match

_LEAD = "Filler opening sentence that carries none of the query terms at all. " * 4


def _shows(snippet: str, query: str) -> bool:
    return text_has_any_match(snippet, MatchSpec.from_query(query))


def test_centres_on_a_stem_variant() -> None:
    body = _LEAD + "Structuring unit testing functions is the topic here." + _LEAD

    assert _shows(_make_snippet(body, "test"), "test")


def test_centres_on_a_wildcard_match() -> None:
    body = _LEAD + "We discuss refactoring at length in this section." + _LEAD

    assert _shows(_make_snippet(body, "refactor*"), "refactor*")


def test_centres_on_a_quoted_phrase() -> None:
    body = _LEAD + "The chapter argues that clean code pays for itself." + _LEAD

    assert _shows(_make_snippet(body, '"clean code"'), '"clean code"')


def test_prefers_an_exact_occurrence_over_a_fuzzy_near_miss() -> None:
    """Auto-fuzzy would happily anchor on "best"; the real "test" wins."""
    body = "best rest west " * 12 + "the actual test appears here" + " nest zest " * 12

    assert "test appears here" in _make_snippet(body, "test")


def test_window_covers_the_most_distinct_terms() -> None:
    """A multi-term query centres where the terms co-occur, not on the first
    lone occurrence."""
    body = "docker " + "filler word " * 40 + "docker compose together" + " filler " * 40

    assert "docker compose together" in _make_snippet(body, "docker compose")


def test_falls_back_to_the_opening_when_nothing_matches() -> None:
    """A chunk with no paintable evidence (heading-only match, filter-only
    query) still gets a readable snippet rather than an empty row."""
    body = "Nothing in this chunk relates to the query at all, but it still reads."

    assert _make_snippet(body, "kubernetes").startswith("Nothing in this chunk")


def test_empty_body_is_empty() -> None:
    assert _make_snippet("", "test") == ""
