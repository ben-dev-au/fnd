"""Quoted phrases highlight as a contiguous span, not word-by-word.

A quoted query like ``"defence in depth"`` must:
* highlight only the contiguous phrase occurrence(s) — so a stopword in
  the phrase (``in``) is NOT lit up everywhere in the document;
* still highlight loose (unquoted) terms word-by-word.
"""

from __future__ import annotations

from fnd.matching import MatchSpec, phrase_char_spans, word_matches


def test_pure_phrase_excludes_words_from_doc_wide_highlight() -> None:
    spec = MatchSpec.from_query('"defence in depth"', auto_fuzzy=False)
    # The phrase is recorded.
    assert spec.phrases
    # Its words do NOT become document-wide single-word matches.
    assert not word_matches("in", spec)
    assert not word_matches("defence", spec)
    assert not word_matches("depth", spec)


def test_phrase_char_spans_finds_contiguous_run() -> None:
    spec = MatchSpec.from_query('"defence in depth"', auto_fuzzy=False)
    text = "Our defence in depth strategy is layered."
    spans = phrase_char_spans(text, spec)
    assert len(spans) == 1
    start, end = spans[0]
    assert text[start:end] == "defence in depth"


def test_phrase_requires_order_and_adjacency() -> None:
    spec = MatchSpec.from_query('"defence in depth"', auto_fuzzy=False)
    assert phrase_char_spans("depth in defence", spec) == []  # reordered
    assert phrase_char_spans("defence and depth", spec) == []  # word missing


def test_phrase_is_stem_aware() -> None:
    spec = MatchSpec.from_query('"monitoring segmentation"', auto_fuzzy=False)
    # Stemmed forms in the doc still match (monitoring→monitor, plural).
    spans = phrase_char_spans("continuous monitoring segmentations here", spec)
    assert len(spans) == 1
    start, end = spans[0]
    assert "monitoring segmentations" in "continuous monitoring segmentations here"[start:end]


def test_loose_terms_still_match_alongside_phrase() -> None:
    spec = MatchSpec.from_query('"defence in depth" segmentation', auto_fuzzy=False)
    assert spec.phrases
    assert word_matches("segmentation", spec)  # loose term highlights doc-wide
    assert not word_matches("in", spec)  # phrase-only word does not


def test_punctuated_phrase_words_still_span() -> None:
    """The user's real case: heading text with '.' and ','."""
    spec = MatchSpec.from_query('"3. Monitoring, segmentation and defence in depth"', auto_fuzzy=False)
    line = "3. Monitoring, segmentation and defence in depth"
    spans = phrase_char_spans(line, spec)
    assert len(spans) == 1
    start, end = spans[0]
    # Span runs from the leading "3" to the trailing "depth".
    assert line[start:end].startswith("3")
    assert line[start:end].endswith("depth")
