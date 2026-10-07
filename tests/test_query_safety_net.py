"""A malformed query is a typed QuerySyntaxError, never a raw ValueError: the
plan refuses malformed structure, and the Searcher converts what Tantivy
itself rejects."""

from __future__ import annotations

from pathlib import Path

import pytest

from fnd.index import build_index
from fnd.query import Searcher
from fnd.query_errors import QuerySyntaxError
from fnd.query_plan import QueryPlan


@pytest.fixture
def built_index(fixtures_dir: Path, tmp_index_dir: Path) -> Path:
    build_index(roots=[fixtures_dir], index_dir=tmp_index_dir, collection="default")
    return tmp_index_dir


@pytest.mark.parametrize("bad", ['"unbalanced', "(foo", "foo)", "a AND", "{60}"])
def test_malformed_structure_is_refused_by_the_plan(bad: str) -> None:
    with pytest.raises(QuerySyntaxError):
        QueryPlan.from_user_text(bad)


@pytest.mark.parametrize("bad", ["page:[10 TO]", "page:abc"])
def test_what_tantivy_rejects_raises_typed_error(built_index: Path, bad: str) -> None:
    searcher = Searcher(index_dir=built_index)
    with pytest.raises(QuerySyntaxError):
        searcher.search(bad, limit=5)


def test_valid_query_still_works(built_index: Path) -> None:
    hits = Searcher(index_dir=built_index).search("blue penguin sandwich", limit=5)
    assert hits


@pytest.mark.parametrize(
    "bad", ["kind:(NOT pdf) blue", "kind:(pdf AND docx) blue", "kind:(-pdf) blue"]
)
def test_logic_inside_an_exact_group_is_refused_not_misread(built_index: Path, bad: str) -> None:
    with pytest.raises(QuerySyntaxError):
        Searcher(index_dir=built_index).search(bad, limit=5)


def test_a_corrupt_stored_chunk_is_not_reported_as_a_query_problem(
    built_index: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fnd.query_errors import QueryError

    def _corrupt(_data: bytes) -> object:
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")

    monkeypatch.setattr("fnd.struct.decode", _corrupt)
    with pytest.raises(UnicodeDecodeError) as caught:
        Searcher(index_dir=built_index)._raw_hits("blue", limit=5, collection=None)
    assert not isinstance(caught.value, QueryError)
