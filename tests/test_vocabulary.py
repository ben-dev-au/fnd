"""The closed value sets behind the CLI's filter validation."""

from __future__ import annotations

import pytest

from fnd.cli_scope import FilterIssues
from fnd.query_errors import UnknownFilterValueError
from fnd.vocabulary import Vocabulary, kind_vocabulary

COLLECTIONS = Vocabulary(
    "collection", ["DPC2", "papers", "Soft Eng Textbooks"], case_sensitive=True
)


def test_exact_name_resolves_to_itself() -> None:
    assert COLLECTIONS.match("papers") == "papers"


def test_surrounding_whitespace_is_ignored() -> None:
    assert COLLECTIONS.match("  papers  ") == "papers"


def test_case_variant_is_not_a_match_when_case_matters() -> None:
    """``F_COLLECTION`` is raw-tokenised — 'dpc2' really would find nothing."""
    assert COLLECTIONS.match("dpc2") is None


def test_case_variant_is_the_leading_suggestion() -> None:
    assert COLLECTIONS.suggest("dpc2")[0] == "DPC2"


def test_short_names_still_suggest_their_case_variant() -> None:
    """A 2-char value gets no edit budget, but zero edits is still zero."""
    tiny = Vocabulary("collection", ["AB"], case_sensitive=True)
    assert tiny.suggest("ab") == ["AB"]
    assert tiny.suggest("xy") == []


def test_transposition_counts_as_one_typo() -> None:
    assert COLLECTIONS.suggest("paeprs") == ["papers"]


def test_a_weaker_candidate_does_not_dilute_an_exact_one() -> None:
    """Real config: 'dpc2' is 'DPC2' bar the case, and 'DPC' plus an edit.
    Offering both would turn an obvious fix into a question."""
    siblings = Vocabulary("collection", ["DPC", "DPC2"], case_sensitive=True)
    assert siblings.suggest("dpc2") == ["DPC2"]
    assert siblings.unknown("dpc2").correction == "DPC2"


def test_nothing_close_suggests_nothing() -> None:
    assert COLLECTIONS.suggest("zzzzzzzz") == []


def test_case_insensitive_vocabulary_resolves_quietly() -> None:
    """Kind clauses are lowercased when compiled, so 'PDF' already works."""
    assert kind_vocabulary().match("PDF") == "pdf"


def test_split_keeps_names_containing_spaces() -> None:
    known, unknown = COLLECTIONS.split_resolve("papers, Soft Eng Textbooks")
    assert known == ["papers", "Soft Eng Textbooks"]
    assert unknown == []


def test_split_separates_the_names_it_cannot_resolve() -> None:
    known, unknown = COLLECTIONS.split_resolve("papers,dpc2")
    assert known == ["papers"]
    assert unknown == ["dpc2"]


def test_resolve_raises_with_the_near_misses_attached() -> None:
    with pytest.raises(UnknownFilterValueError) as exc:
        COLLECTIONS.resolve("dpc2", flag="--collection")
    assert exc.value.value == "dpc2"
    assert exc.value.flag == "--collection"
    assert exc.value.correction == "DPC2"
    assert exc.value.hint == "did you mean 'DPC2'?"


def test_an_ambiguous_value_offers_no_single_correction() -> None:
    pair = Vocabulary("collection", ["cat", "bat"], case_sensitive=True)
    err = pair.unknown("hat")
    assert set(err.suggestions) == {"cat", "bat"}
    assert err.correction is None


# ── the collector ─────────────────────────────────────────────────────────


def test_a_fresh_collector_is_falsy() -> None:
    assert not FilterIssues()


def test_resolve_records_instead_of_raising() -> None:
    """One pass has to reach the end of the command line before reporting."""
    issues = FilterIssues()
    assert issues.resolve(COLLECTIONS, "dpc2", flag="--collection") == "DPC2"
    assert issues.resolve(COLLECTIONS, "papers") == "papers"
    assert len(issues.issues) == 1


def test_an_uncorrectable_value_comes_back_unchanged() -> None:
    issues = FilterIssues()
    assert issues.resolve(COLLECTIONS, "zzzzzzzz") == "zzzzzzzz"
    assert issues.issues[0].correction is None


def test_check_records_without_offering_a_fix() -> None:
    issues = FilterIssues()
    issues.check(COLLECTIONS, "dpc2")
    assert issues.issues[0].flag is None, "an unflagged issue is never auto-applied"


def test_split_resolve_carries_corrections_through() -> None:
    issues = FilterIssues()
    assert issues.split_resolve(COLLECTIONS, "dpc2,papers", flag="--collection") == [
        "papers",
        "DPC2",
    ]
    assert len(issues.issues) == 1


def test_punctuation_only_scope_is_an_error_not_a_silent_widening() -> None:
    issues = FilterIssues()
    assert issues.split_resolve(COLLECTIONS, ",", flag="--collection") == []
    assert len(issues.issues) == 1
