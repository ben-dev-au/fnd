"""The Searcher must turn any malformed query Tantivy rejects into a typed
QuerySyntaxError instead of letting a raw ValueError crash the caller."""

from __future__ import annotations

from pathlib import Path

import pytest

from fnd.index import build_index
from fnd.query import Searcher
from fnd.query_errors import QuerySyntaxError


@pytest.fixture
def built_index(fixtures_dir: Path, tmp_index_dir: Path) -> Path:
    build_index(roots=[fixtures_dir], index_dir=tmp_index_dir, collection="default")
    return tmp_index_dir


@pytest.mark.parametrize(
    "bad",
    [
        '"unbalanced',  # unclosed quote
        "(foo",  # unbalanced paren
        "foo)",
        "a AND",  # dangling boolean
        "page:[10 TO]",  # malformed range
        "page:abc",  # non-integer range value
        "{60}",  # malformed proximity that never reached the planner
    ],
)
def test_malformed_query_raises_typed_error(built_index: Path, bad: str) -> None:
    searcher = Searcher(index_dir=built_index)
    with pytest.raises(QuerySyntaxError):
        searcher.search(bad, limit=5)


def test_valid_query_still_works(built_index: Path) -> None:
    hits = Searcher(index_dir=built_index).search("blue penguin sandwich", limit=5)
    assert hits
