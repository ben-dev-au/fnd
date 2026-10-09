"""A tantivy panic is a ``BaseException`` that escapes every ``except Exception``;
at the search boundary it becomes a typed query error, never a dead search."""

from __future__ import annotations

from pathlib import Path

import pytest
import tantivy

from fnd.index import build_index
from fnd.query import Searcher
from fnd.query_errors import QueryEngineError, QuerySyntaxError
from fnd.schema import F_BODY, build_schema


def _a_real_panic() -> BaseException:
    try:
        tantivy.Query.regex_phrase_query(build_schema(), F_BODY, ["a.*"], slop=0)
    except BaseException as e:
        return e
    raise AssertionError("tantivy no longer panics on a one-term regex phrase")


@pytest.fixture
def searcher(tmp_path: Path) -> Searcher:
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "a.md").write_text("# A\n\nsaffron risotto\n", encoding="utf-8")
    build_index(roots=[docs], index_dir=tmp_path / "idx", collection="c", tag_sources=())
    return Searcher(index_dir=tmp_path / "idx")


def test_a_panic_while_searching_is_a_query_error(
    searcher: Searcher, monkeypatch: pytest.MonkeyPatch
) -> None:
    panic = _a_real_panic()
    assert not isinstance(panic, Exception)

    def _panics(*_args: object, **_kwargs: object) -> object:
        raise panic

    monkeypatch.setattr("fnd.query_compile.compile_query", _panics)
    with pytest.raises(QueryEngineError) as caught:
        searcher._candidates("saffron", window=5, collection=None)
    assert isinstance(caught.value, QuerySyntaxError)
    assert caught.value.hint


def test_an_interrupt_still_interrupts(searcher: Searcher, monkeypatch: pytest.MonkeyPatch) -> None:
    def _interrupted(*_args: object, **_kwargs: object) -> object:
        raise KeyboardInterrupt

    monkeypatch.setattr("fnd.query_compile.compile_query", _interrupted)
    with pytest.raises(KeyboardInterrupt):
        searcher._candidates("saffron", window=5, collection=None)


def test_a_query_tantivy_refuses_only_when_run_is_a_query_error(
    searcher: Searcher, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _refused(*_args: object, **_kwargs: object) -> object:
        raise ValueError("Phrase query exceeded max expansions")

    monkeypatch.setattr("fnd.query_compile.compile_query", _refused)
    with pytest.raises(QueryEngineError):
        searcher._candidates("saffron", window=5, collection=None)


def test_a_query_error_passes_through_unchanged(
    searcher: Searcher, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _syntax(*_args: object, **_kwargs: object) -> object:
        raise QuerySyntaxError("unclosed quote")

    monkeypatch.setattr("fnd.query_compile.compile_query", _syntax)
    with pytest.raises(QuerySyntaxError) as caught:
        searcher._candidates("saffron", window=5, collection=None)
    assert not isinstance(caught.value, QueryEngineError)
