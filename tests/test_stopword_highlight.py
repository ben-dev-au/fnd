"""Stopwords add highlight noise with no value — don't bold them doc-wide.

A query like ``segmentation and defence in depth`` should highlight the content
words but not light up every ``and`` / ``in`` in the document. A *quoted* phrase
still highlights its stopwords, but only as part of the contiguous span.
"""

from __future__ import annotations

from fnd.matching import MatchSpec, phrase_char_spans, word_matches
from fnd.render import _terms_from_query


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
    # hyphenated form tokenizes to defence/in/depth — same contiguous run.
    assert _stopword_lit("the defence-in-depth model", spec, "in")


def test_incontext_stopword_lit_in_partial_fragment() -> None:
    """'adjacent to any match' — a fragment with one content neighbour still
    lights the connector (user's 'in depth' / 'defence in' cases)."""
    spec = MatchSpec.from_query("defence in depth", auto_fuzzy=False)
    assert _stopword_lit("research in depth here", spec, "in")  # in+depth
    assert _stopword_lit("mount a strong defence in court", spec, "in")  # defence+in


def test_stopword_not_lit_without_a_matched_neighbour() -> None:
    spec = MatchSpec.from_query("defence in depth", auto_fuzzy=False)
    assert not _stopword_lit("please stand in line", spec, "in")
    assert not word_matches("in", spec)  # standalone is still dark
