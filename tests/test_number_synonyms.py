"""Number<->word synonym groups (cardinals + ordinals).

`4` matches `four`, `1st` matches `first`, bidirectionally — but a number
inside a user's quoted phrase stays literal (the exact-match request wins).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fnd.fusion import fusion_search
from fnd.index import build_index
from fnd.number_synonyms import build_number_table
from fnd.query import Searcher
from fnd.synonyms import expand, load_default_synonyms


def test_cardinal_group_is_bidirectional():
    table = build_number_table()
    grp = table.expansions_for("4")
    assert grp is not None
    assert {t.casefold() for t in grp} == {"4", "four"}
    assert table.expansions_for("four") == grp


def test_cardinal_range_endpoints_and_tens():
    table = build_number_table()
    for digit, word in (
        ("0", "zero"),
        ("20", "twenty"),
        ("90", "ninety"),
        ("100", "hundred"),
        ("1000", "thousand"),
    ):
        grp = table.expansions_for(digit)
        assert grp is not None, digit
        assert word in {t.casefold() for t in grp}


def test_ordinal_group_is_bidirectional():
    table = build_number_table()
    for ordform, word in (
        ("1st", "first"),
        ("2nd", "second"),
        ("3rd", "third"),
        ("4th", "fourth"),
        ("20th", "twentieth"),
        ("90th", "ninetieth"),
    ):
        grp = table.expansions_for(ordform)
        assert grp is not None, ordform
        assert {t.casefold() for t in grp} == {ordform, word}


def test_cardinal_and_ordinal_are_distinct_groups():
    table = build_number_table()
    cardinal = table.expansions_for("4")
    ordinal = table.expansions_for("4th")
    assert cardinal is not None
    assert ordinal is not None
    assert "fourth" not in {t.casefold() for t in cardinal}
    assert "four" not in {t.casefold() for t in ordinal}


def test_expand_digit_to_word():
    table = build_number_table()
    out = expand("4 horsemen", table)
    assert out == "(4 OR four) horsemen"


def test_expand_word_to_digit():
    table = build_number_table()
    out = expand("four horsemen", table)
    assert "4" in out
    assert "four" in out
    assert out.endswith(" horsemen")


def test_quoted_number_is_not_expanded():
    table = build_number_table()
    assert expand('"4" horsemen', table) == '"4" horsemen'


def test_expand_ordinal():
    table = build_number_table()
    assert expand("1st place", table) == "(1st OR first) place"
    out = expand("first place", table)
    assert "1st" in out
    assert out.endswith(" place")


def test_numbers_present_in_default_table():
    """The bundled default table ships numbers alongside the curated acronyms."""
    table = load_default_synonyms()
    assert table.expansions_for("4") is not None
    assert table.expansions_for("first") is not None
    # acronyms still present
    assert table.expansions_for("mfa") is not None


@pytest.fixture
def digits_index(tmp_path: Path, tmp_index_dir: Path) -> Path:
    """A one-file index whose body holds digit forms (4, 1st), so we can prove
    Tantivy tokenizes them as searchable and the synonym disjunction matches."""
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "horsemen.md").write_text(
        "# Riders\n\nThe 4 horsemen ride out at the 1st hour of dusk.\n",
        encoding="utf-8",
    )
    build_index(roots=[docs], index_dir=tmp_index_dir, collection="default")
    return tmp_index_dir


def test_word_query_finds_digit_in_index(digits_index: Path) -> None:
    """Searching the spelled word surfaces a doc that only contains the digit."""
    s = Searcher(index_dir=digits_index)
    table = load_default_synonyms()
    hits = fusion_search(s, query="four", synonyms=table, limit=10)
    assert any(h.path.endswith("horsemen.md") for h in hits)


def test_ordinal_word_query_finds_digit_in_index(digits_index: Path) -> None:
    s = Searcher(index_dir=digits_index)
    table = load_default_synonyms()
    hits = fusion_search(s, query="first", synonyms=table, limit=10)
    assert any(h.path.endswith("horsemen.md") for h in hits)


def test_quoted_word_query_does_not_synonym_match(digits_index: Path) -> None:
    """Quoting suppresses expansion: literal `four` is absent from the doc
    (which only has `4`), so the quoted query returns no synonym hit."""
    s = Searcher(index_dir=digits_index)
    table = load_default_synonyms()
    hits = fusion_search(s, query='"four"', synonyms=table, limit=10)
    assert not any(h.path.endswith("horsemen.md") for h in hits)
