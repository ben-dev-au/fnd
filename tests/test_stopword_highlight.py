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
