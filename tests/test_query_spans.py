"""``fnd.query_spans``: one answer to where a query holds a quoted phrase or a
``/regex/``, shared by every pass that rewrites the query string."""

from __future__ import annotations

import itertools

import pytest

from fnd.query_spans import Span, has_phrase, literal_spans, map_outside, without_literals


def _spans(query: str) -> list[tuple[str, str]]:
    return [(span.kind, query[span.start : span.end]) for span in literal_spans(query)]


@pytest.mark.parametrize(
    ("query", "spans"),
    [
        ("don't kind:pdf", []),
        ("O'Reilly's book", []),
        ("'quick brown' x", [("phrase", "'quick brown'")]),
        ("'rock'n'roll' x", [("phrase", "'rock'n'roll'")]),
        ('"a b" c', [("phrase", '"a b"')]),
        ('c:"Book Club" x', [("phrase", '"Book Club"')]),
        ('("a b") x', [("phrase", '"a b"')]),
        ('-"a b"', [("phrase", '"a b"')]),
        ('"a b"~3 c', [("phrase", '"a b"')]),
        ('"a b', [("phrase", '"a b')]),
        ("'90s music", []),
        ("quick 'brown", []),
        ("/cr[y]pto/ x", [("regex", "/cr[y]pto/")]),
        ("/[0-9]{4}/~2", [("regex", "/[0-9]{4}/")]),
        ("+/x y/ z", [("regex", "/x y/")]),
        (r"/a\/b/ c", [("regex", r"/a\/b/")]),
        ("a/b/c", []),
        ("/usr/bin", []),
        ("and/or", []),
        ("https://example.com/page", []),
    ],
)
def test_literal_spans(query: str, spans: list[tuple[str, str]]) -> None:
    assert _spans(query) == spans


def test_map_outside_leaves_literals_alone() -> None:
    query = 'x /a{2}/ "b {3}" y'
    assert map_outside(query, str.upper) == 'X /a{2}/ "b {3}" Y'


def test_map_outside_can_leave_one_kind_to_the_caller() -> None:
    query = '{3} "a b" /c{2}/'
    assert map_outside(query, lambda s: s.replace("{", "<"), kinds={"regex"}) == '<3} "a b" /c{2}/'


def test_a_span_is_ordered_and_non_overlapping() -> None:
    spans = literal_spans("'a b' \"c\" /d/ 'e'")
    assert spans == tuple(sorted(spans, key=lambda s: s.start))
    assert all(a.end <= b.start for a, b in itertools.pairwise(spans))
    assert all(isinstance(s, Span) for s in spans)


def test_without_literals_keeps_each_span_as_a_bare_word() -> None:
    assert without_literals('a "b (c" /d)/ e').split() == ["a", "_", "_", "e"]


@pytest.mark.parametrize(
    ("query", "phrase"),
    [("'cross entropy' loss", True), ('"a b" c', True), ("don't panic", False), ("/a b/", False)],
)
def test_has_phrase(query: str, phrase: bool) -> None:
    assert has_phrase(query) is phrase


def test_a_single_quoted_phrase_is_precision_to_search_too() -> None:
    from fnd.cascade import _carries_precision_intent

    assert _carries_precision_intent("'cross entropy' loss")
    assert not _carries_precision_intent("don't panic")


@pytest.mark.parametrize("opener", ["'a ", "/a "])
def test_many_unclosed_openers_scan_in_linear_time(opener: str) -> None:
    import time

    query = opener * 2_700  # ~8 KB, the planner's size limit
    started = time.perf_counter()
    assert literal_spans(query) == ()
    assert time.perf_counter() - started < 0.5
