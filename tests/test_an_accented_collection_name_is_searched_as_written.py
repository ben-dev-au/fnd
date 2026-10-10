"""A collection name with an accent reaches the index as written, wherever the
``c:`` clause sits, while patterns (typed in ASCII or full width) still match
words with their accents folded."""

from __future__ import annotations

from pathlib import Path

import pytest

from fnd import query_dsl
from fnd.index import build_index
from fnd.query import Searcher
from fnd.query_errors import QuerySyntaxError
from fnd.query_plan import QueryPlan


@pytest.fixture
def searcher(tmp_path: Path, tmp_index_dir: Path) -> Searcher:
    for name, body in (("Café", "Crémant tasting notes."), ("work", "Meeting notes.")):
        root = tmp_path / name
        root.mkdir()
        (root / f"{name}.md").write_text(f"# {name}\n\n{body}\n", encoding="utf-8")
        build_index(roots=[root], index_dir=tmp_index_dir, collection=name)
    return Searcher(index_dir=tmp_index_dir)


def _names(searcher: Searcher, query: str) -> set[str]:
    return {Path(h.path).stem for h in searcher.search(query, limit=10)}


def test_the_shorthand_keeps_every_letter_of_a_bare_name() -> None:
    assert query_dsl.preprocess("c:Café notes") == 'collection:"Café" notes'


@pytest.mark.parametrize(
    "query", ["c:Café notes", "c:Café AND notes", "notes AND c:Café", "(c:Café) AND notes"]
)
def test_an_accented_collection_clause_finds_its_files(searcher: Searcher, query: str) -> None:
    assert _names(searcher, query) == {"Café"}


@pytest.mark.parametrize("query", ["crém*", "*émant", "/crém[a-z]nt/", '"crém* tasting"'])
def test_an_accented_pattern_matches_the_folded_word(searcher: Searcher, query: str) -> None:
    assert _names(searcher, query) == {"Café"}


@pytest.mark.parametrize(
    "query",
    [
        "crém＊",
        "tasting AND （crémant OR meeting）",
        "title：Café notes",
        "notes －meeting",
    ],
)
def test_full_width_syntax_reads_as_its_ascii_form(searcher: Searcher, query: str) -> None:
    assert _names(searcher, query) == {"Café"}


def test_full_width_brackets_stay_search_text() -> None:
    """As ``[]`` they would turn the word into a frontmatter filter with no search."""
    plan = QueryPlan.from_user_text("［tasting］")
    assert plan.metadata_filter is None
    assert "tasting" in plan.lexical


@pytest.mark.parametrize(
    ("typed", "read"),
    [("page:［1 TO 3］ notes", "page:[1 TO 3] notes"), ("page：［0 TO 3｝", "page:[0 TO 3}")],
)
def test_full_width_brackets_after_a_colon_are_a_range(typed: str, read: str) -> None:
    assert QueryPlan.from_user_text(typed).lexical == read


def test_a_frontmatter_value_keeps_its_full_width_punctuation() -> None:
    """The note's frontmatter holds the ``：`` as written, so the filter must too."""
    plan = QueryPlan.from_user_text("notes： [Title == '会議：議事録']")
    assert plan.metadata_filter == "Title == '会議：議事録'"
    assert plan.lexical.strip() == "notes:"


def test_an_unclosed_full_width_quote_is_refused() -> None:
    with pytest.raises(QuerySyntaxError):
        QueryPlan.from_user_text("＂crémant")


def test_a_frontmatter_value_keeps_its_full_width_quotes() -> None:
    plan = QueryPlan.from_user_text("notes [Author == 'O＇Neil']")
    assert plan.metadata_filter == "Author == 'O＇Neil'"
