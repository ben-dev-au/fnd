"""Query terms with adjacent punctuation still highlight.

The document side extracts words with ``re.finditer(r"\\w+")`` (no
punctuation). ``_terms_from_query`` split the query on whitespace only,
so a term like ``3.`` or ``Monitoring,`` kept its trailing punctuation;
its stem (``3.`` / ``monitoring,``) then never equalled the clean
doc-word stem (``3`` / ``monitor``) — so those words were silently left
un-highlighted while neighbouring clean words lit up.
"""

from __future__ import annotations

from fnd.matching import MatchSpec, word_matches
from fnd.render import _terms_from_query

PHRASE = '"3. Monitoring, segmentation and defence in depth"'


def test_terms_have_no_adjacent_punctuation() -> None:
    terms = _terms_from_query(PHRASE)
    assert "3" in terms
    assert "Monitoring" in terms
    # No term should carry punctuation.
    assert not any(any(c in t for c in ".,;:") for t in terms), terms


def test_punctuated_unquoted_terms_match_doc_words() -> None:
    """Unquoted terms with adjacent punctuation still highlight doc-wide.

    (For a *quoted* phrase the words highlight via the phrase span, not
    document-wide — covered in test_phrase_matching.)"""
    spec = MatchSpec.from_query("3. Monitoring, segmentation depth", auto_fuzzy=True)
    # The tokens that sat before punctuation.
    assert word_matches("Monitoring", spec)
    assert word_matches("3", spec)
    # And the clean ones still match.
    assert word_matches("segmentation", spec)
    assert word_matches("depth", spec)


def test_hyphenated_term_matches_doc_word() -> None:
    """A hyphenated query term aligns with the doc side's \\w+ split."""
    spec = MatchSpec.from_query("defence-in-depth", auto_fuzzy=False)
    assert word_matches("defence", spec)
    assert word_matches("depth", spec)
