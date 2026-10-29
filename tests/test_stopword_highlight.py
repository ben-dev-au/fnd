"""Stopwords add highlight noise with no value: don't bold them doc-wide.

A query like ``segmentation and defence in depth`` should highlight the content
words but not light up every ``and`` / ``in`` in the document. A *quoted* phrase
still highlights its stopwords, but only as part of the contiguous span.
"""

from __future__ import annotations

import pytest

from fnd.matching import MatchSpec, phrase_char_spans, word_matches
from fnd.render import (
    _terms_from_query,
    match_word_spans,
    match_word_spans_multi,
    text_has_any_match,
)


def test_terms_from_query_drops_stopwords() -> None:
    assert _terms_from_query("defence in depth and segmentation") == [
        "defence",
        "depth",
        "segmentation",
    ]


def test_unquoted_stopwords_not_highlighted_doc_wide() -> None:
    spec = MatchSpec.from_query("segmentation and defence in depth", auto_fuzzy=False)
    assert word_matches("segmentation", spec)
    assert word_matches("defence", spec)
    assert word_matches("depth", spec)
    assert not word_matches("and", spec)
    assert not word_matches("in", spec)


def test_quoted_phrase_still_highlights_its_stopwords_in_span() -> None:
    spec = MatchSpec.from_query('"defence in depth"', auto_fuzzy=False)
    spans = phrase_char_spans("a layered defence in depth model", spec)
    assert spans, "the contiguous phrase (including 'in') should highlight"
    # …but a stray 'in' elsewhere is not a doc-wide loose term.
    assert not word_matches("in", spec)


def test_content_word_that_looks_short_is_kept() -> None:
    # 'key' / 'depth' are content, not stopwords.
    assert "key" in _terms_from_query("key rotation")


def _stopword_lit(text: str, spec: MatchSpec, word: str) -> bool:
    """True if a whole-word occurrence of ``word`` falls inside a phrase span."""
    import re

    spans = phrase_char_spans(text, spec)
    return any(
        m.group(0).lower() == word and any(s <= m.start() and m.end() <= e for s, e in spans)
        for m in re.finditer(r"\w+", text)
    )


def test_incontext_stopword_lit_between_two_matches() -> None:
    spec = MatchSpec.from_query("defence in depth", auto_fuzzy=False)
    assert _stopword_lit("a layered defence in depth model", spec, "in")
    # hyphenated form tokenizes to defence/in/depth: same contiguous run.
    assert _stopword_lit("the defence-in-depth model", spec, "in")


def test_incontext_stopword_lit_in_partial_fragment() -> None:
    """'adjacent to any match': a fragment with one content neighbour still
    lights the connector (user's 'in depth' / 'defence in' cases)."""
    spec = MatchSpec.from_query("defence in depth", auto_fuzzy=False)
    assert _stopword_lit("research in depth here", spec, "in")  # in+depth
    assert _stopword_lit("mount a strong defence in court", spec, "in")  # defence+in


def test_stopword_not_lit_without_a_matched_neighbour() -> None:
    spec = MatchSpec.from_query("defence in depth", auto_fuzzy=False)
    assert not _stopword_lit("please stand in line", spec, "in")
    assert not word_matches("in", spec)  # standalone is still dark


@pytest.mark.parametrize("query", ['javascript "OR"', 'javascript "or"', 'java "in" ruby'])
def test_quoted_stopword_beside_a_content_word_paints(query: str) -> None:
    """A quoted stopword is searched, so a chunk matching only it must be paintable."""
    spec = MatchSpec.from_query(query, auto_fuzzy=False)
    word = "in" if '"in"' in query else "or"
    assert text_has_any_match(f"Take the red pill {word} the blue pill.", spec)


def test_unquoted_stopword_beside_a_content_word_stays_dark() -> None:
    spec = MatchSpec.from_query("javascript or", auto_fuzzy=False)
    assert not text_has_any_match("Take the red pill or the blue pill.", spec)


@pytest.mark.parametrize("query", ['javascript "or,"', 'javascript "(or)"', 'javascript "or."'])
def test_quoted_stopword_with_punctuation_paints(query: str) -> None:
    spec = MatchSpec.from_query(query, auto_fuzzy=False)
    assert text_has_any_match("Take the red pill or the blue pill.", spec)


@pytest.mark.parametrize(
    "query",
    ['java NOT "or"', 'java -"or"', 'NOT "or"', 'NOT (java "or")', 'title:(java "or")'],
)
def test_negated_or_field_scoped_quoted_stopword_does_not_paint(query: str) -> None:
    spec = MatchSpec.from_query(query, auto_fuzzy=False)
    assert not text_has_any_match("Take the red pill or the blue pill.", spec)


def test_a_nul_in_the_query_is_not_read_as_a_quoted_stopword() -> None:
    spec = MatchSpec.from_query("java \x00running", auto_fuzzy=False)
    assert spec.phrases == ()


def _painted(text: str, query: str) -> list[str]:
    spec = MatchSpec.from_query(query, auto_fuzzy=False)
    return [text[a:b].lower() for a, b, _ in match_word_spans(text, spec)]


_FAR = " ".join(["filler"] * 30)


@pytest.mark.parametrize(
    "query", ['{10}javascript "OR"', "{10}javascript or", '{10}javascript "or"']
)
def test_stopword_group_member_paints_inside_the_window_only(query: str) -> None:
    text = f"Use javascript or python today. {_FAR} Pick red or blue."
    assert _painted(text, query) == ["javascript", "or"]


@pytest.mark.parametrize("query", ["{3} or the", '"or the"~3', '{10}javascript "OR"'])
def test_a_painted_stopword_group_counts_as_a_match(query: str) -> None:
    assert text_has_any_match("Take javascript or the blue pill.", MatchSpec.from_query(query))


@pytest.mark.parametrize("query", ["{3} or the", '{10}javascript "OR"'])
def test_a_stopword_outside_any_window_is_not_a_match(query: str) -> None:
    """Nothing paints it, so a marker or n/b stop must not point at it."""
    assert not text_has_any_match("Pick red or blue.", MatchSpec.from_query(query))


def test_a_stopword_group_member_paints_across_a_line_break() -> None:
    spec = MatchSpec.from_query('{10}javascript "OR"', auto_fuzzy=False)
    assert match_word_spans_multi(["Use javascript daily.", "or python"], spec)[1]


@pytest.mark.parametrize("query", ['{10}javascript "OR"', "{10}javascript or", "{10}or javascript"])
def test_a_stopword_group_member_has_its_own_colour(query: str) -> None:
    """Each term of a multi-term query paints in its own colour, stopword members included."""
    text = "Use javascript or python today."
    spec = MatchSpec.from_query(query, auto_fuzzy=False)
    styles = {text[a:b].lower(): style for a, b, style in match_word_spans(text, spec)}
    assert set(styles) == {"javascript", "or"}
    assert styles["javascript"] != styles["or"]


@pytest.mark.parametrize(
    "query", ['{10}javascript "OR"', "{10}or javascript", '{3}javascript "OR" python']
)
def test_a_stopword_group_member_adds_no_adjacency_phrase(query: str) -> None:
    """A window paints it, so an adjacent-pair phrase would only paint it, and the space, out of window."""
    spec = MatchSpec.from_query(query, auto_fuzzy=False)
    assert phrase_char_spans("Pick red or python, or javascript or more.", spec) == []


@pytest.mark.parametrize("query", ["defence in depth {5}x in y", "defence in depth {5}a in b"])
def test_a_free_stopword_keeps_its_connector_when_a_group_repeats_it(query: str) -> None:
    spec = MatchSpec.from_query(query, auto_fuzzy=False)
    assert phrase_char_spans("defence in depth matters", spec)


@pytest.mark.parametrize(
    ("query", "text"),
    [
        ("javascript the {10}or python", "Pick the or blue."),
        ('javascript in {3}"or" the', "Pick in or blue."),
    ],
)
def test_a_free_stopword_does_not_pair_with_a_group_stopword(query: str, text: str) -> None:
    spec = MatchSpec.from_query(query, auto_fuzzy=False)
    assert phrase_char_spans(text, spec) == []
    assert not text_has_any_match(text, spec)
