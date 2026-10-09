"""``fnd.query_escape``: user text written into tantivy's query-string syntax
reads as the words typed, never as operators."""

from __future__ import annotations

import re

import pytest
import tantivy

from fnd.analysis import register
from fnd.query_escape import literal, literal_phrase
from fnd.schema import F_BODY, build_schema
from tests import _hostile_text

_TEXT = [
    "don't",
    "O'Reilly",
    "10:30",
    "TODO:",
    "https://example.com/page",
    'a"b',
    "back\\slash",
    "(x)",
    "[y]",
    "{z}",
    "a^2",
    "q~1",
    "c++",
    "-x",
    "+x",
    "a&&b||c!",
    "x<y>=z",
    "*",
    "?",
    "/re/",
    *_hostile_text.ALL,
]


@pytest.fixture(scope="module")
def index() -> tantivy.Index:
    return register(tantivy.Index(build_schema()))


def _terms(index: tantivy.Index, query: str) -> list[str]:
    return re.findall(r'type=Str, "([^"]*)"', str(index.parse_query(query, [F_BODY])))


@pytest.mark.parametrize("text", _TEXT)
def test_a_literal_parses_as_the_words_typed(index: tantivy.Index, text: str) -> None:
    words = "".join(ch if ch.isalnum() else " " for ch in text).split()
    assert set(_terms(index, literal(text))) == set(_terms(index, " ".join(words)))


@pytest.mark.parametrize("text", _TEXT)
def test_a_literal_phrase_parses(index: tantivy.Index, text: str) -> None:
    _terms(index, literal_phrase(text))
    _terms(index, literal_phrase(text, slop=3))


def test_a_phrase_keeps_its_slop(index: tantivy.Index) -> None:
    assert "slop: 3" in str(index.parse_query(literal_phrase("cross entropy", slop=3), [F_BODY]))
