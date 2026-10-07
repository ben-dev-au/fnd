"""Quoted phrases highlight as a contiguous span, not word-by-word.

A quoted query like ``"defence in depth"`` must:
* highlight only the contiguous phrase occurrence(s) — so a stopword in
  the phrase (``in``) is NOT lit up everywhere in the document;
* still highlight loose (unquoted) terms word-by-word.
"""

from __future__ import annotations

from fnd.matching import MatchSpec, match_color, phrase_char_spans, word_matches
from fnd.render import MISMATCH_STYLE, match_word_spans, word_highlight_runs
from fnd.synonyms import SynonymTable


def test_pure_phrase_excludes_words_from_doc_wide_highlight() -> None:
    spec = MatchSpec.from_query('"defence in depth"', auto_fuzzy=False)
    # The phrase is recorded.
    assert spec.phrases
    # Its words do NOT become document-wide single-word matches.
    assert not word_matches("in", spec)
    assert not word_matches("defence", spec)
    assert not word_matches("depth", spec)


def test_quoted_underscore_identifier_stays_a_phrase_not_loose_terms() -> None:
    # The index analyser's split (DOC_WORD_RE) makes a quoted underscore identifier a
    # multi-token phrase. Its parts must NOT leak into the loose (doc-wide) term
    # set — otherwise quoting "recursive_directory_iterator" would light up
    # 'recursive'/'directory'/'iterator' everywhere, breaking the phrase contract.
    spec = MatchSpec.from_query('"recursive_directory_iterator"', auto_fuzzy=False)
    assert spec.phrases  # recorded as a contiguous phrase
    assert not spec.exact_stems  # but no loose, doc-wide single-word matches
    assert not word_matches("iterator", spec)
    assert not word_matches("recursive", spec)


def test_single_word_quote_still_folds_into_loose_terms() -> None:
    # A genuine single-token quote is the same as the bare word and must still
    # highlight word-by-word (its DOC_WORD_RE token count is 1).
    spec = MatchSpec.from_query('"powerhouse"', auto_fuzzy=False)
    assert word_matches("powerhouse", spec)


def test_single_word_quote_paints_only_the_exact_word() -> None:
    """A quote stands search's fuzzy and synonym passes down, so the paint must too."""
    synonyms = SynonymTable.from_groups([["tax", "levy"]])
    spec = MatchSpec.from_query('"TAX"', synonyms=synonyms)
    assert word_matches("TAX", spec)
    assert not word_matches("wax", spec)
    assert not word_matches("levy", spec)


def test_quote_elsewhere_stops_loose_terms_widening() -> None:
    """Search skips fuzzy for the whole query once it carries a quote."""
    spec = MatchSpec.from_query('"defence in depth" theory')
    assert word_matches("theory", spec)
    assert not word_matches("theora", spec)


def test_explicit_fuzz_survives_a_quote() -> None:
    """The lex pass compiles a typed ``~N`` whatever else the query carries."""
    assert word_matches("theora", MatchSpec.from_query('"defence in depth" theory~1'))


def test_bare_word_still_fuzzes() -> None:
    """Without precision intent search fuzzes, so the paint does too."""
    assert word_matches("tux", MatchSpec.from_query("TAX"))


def test_fuzz_keeps_searchs_first_letter() -> None:
    """Search's fuzzy pass only scans words sharing the query's first letter."""
    spec = MatchSpec.from_query("TAX")
    assert not word_matches("wax", spec)
    starts = {a for a, _b, _style in match_word_spans("wax tux", spec)}
    assert starts
    assert min(starts) >= len("wax ")


def test_colour_follows_the_term_search_fuzzed() -> None:
    """A word takes the colour of the term search's fuzzy pass actually reached."""
    assert match_color("bax", MatchSpec.from_query("tax bar")) == 1


def test_no_typo_mark_from_a_term_search_never_fuzzed() -> None:
    """A regex hit is not split into a typo of an unrelated fuzzy term."""
    runs = word_highlight_runs("wax", MatchSpec.from_query("tax /w.x/"))
    assert [(a, b) for a, b, _style in runs] == [(0, 3)]
    assert all(style != MISMATCH_STYLE for _a, _b, style in runs)


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
    spec = MatchSpec.from_query(
        '"3. Monitoring, segmentation and defence in depth"', auto_fuzzy=False
    )
    line = "3. Monitoring, segmentation and defence in depth"
    spans = phrase_char_spans(line, spec)
    assert len(spans) == 1
    start, end = spans[0]
    # Span runs from the leading "3" to the trailing "depth".
    assert line[start:end].startswith("3")
    assert line[start:end].endswith("depth")
