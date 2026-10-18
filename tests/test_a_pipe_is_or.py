"""``a|b`` is shorthand for the group ``(a OR b)``; quoted and regex text keeps its ``|``."""

from __future__ import annotations

import pytest

from fnd.query_errors import QuerySyntaxError
from fnd.query_plan import QueryPlan


@pytest.mark.parametrize(
    ("typed", "lexical"),
    [
        ("maven|mvn commands", "(maven OR mvn) commands"),
        ("maven|mvn|mvnw", "(maven OR mvn OR mvnw)"),
        ("maven||mvn", "(maven OR mvn)"),
        ("maven | mvn", "maven OR mvn"),
        ("maven || mvn", "maven OR mvn"),
        ("title:zephyr|quokka", "title:(zephyr OR quokka)"),
        ("-maven|mvn build", "-(maven OR mvn) build"),
        ("+maven|mvn build", "+(maven OR mvn) build"),
        ("{5} maven|mvn commands", "{5} (maven OR mvn) commands"),
        ("zephyr｜harbour", "(zephyr OR harbour)"),
        ('"maven|mvn" commands', '"maven|mvn" commands'),
        ("/maven|mvn/ commands", "/maven|mvn/ commands"),
        ('"apache maven"|mvn', '"apache maven" OR mvn'),
    ],
)
def test_a_pipe_reads_as_or(typed: str, lexical: str) -> None:
    assert QueryPlan.from_user_text(typed).lexical == lexical


@pytest.mark.parametrize("typed", ["maven|", "|maven", "maven | ", "maven || commands |"])
def test_a_pipe_without_a_word_beside_it_is_refused(typed: str) -> None:
    with pytest.raises(QuerySyntaxError, match=r"\|"):
        QueryPlan.from_user_text(typed)
