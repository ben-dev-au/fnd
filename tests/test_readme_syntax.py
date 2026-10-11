"""Lock the README §"Search how-to" examples to actual behaviour: every
documented query expands to the documented form and parses in Tantivy without
error. Keeps the docs and the engine from drifting apart."""

from __future__ import annotations

import pytest
import tantivy

from fnd import query_dsl
from fnd.analysis import register
from fnd.query_plan import QueryPlan
from fnd.schema import F_BODY, build_schema

# (documented input, expected DSL translation). Time-relative forms (mtime
# tokens / ISO compares) are covered separately in test_query_dsl.
DOCUMENTED_TRANSLATIONS = [
    ("{5} project deadline", '"project deadline"~5'),
    ("project NEAR/5 deadline", '"project deadline"~5'),
    ("{20} cancel the gym membership", '"cancel the gym membership"~20'),
    ("{60} lost luggage claim", '"lost luggage claim"~60'),
    ("{500} kitchen renovation quote", '"kitchen renovation quote"~500'),
    # Worked example (README composing section): proximity stops at the qualifier.
    ("{10} lost luggage claim kind:pdf", '"lost luggage claim"~10 kind:pdf'),
    ("c:work deadline", 'collection:"work" deadline'),
    ("c:work,home budget", '(collection:"work" OR collection:"home") budget'),
    ("page:>20", f"page:[21 TO {query_dsl.FAR_FUTURE}]"),
    ("slide:<5", "slide:[1 TO 4]"),
]

# Documented inputs Tantivy/our DSL pass through unchanged.
DOCUMENTED_NATIVE = [
    "invoice",
    "holiday budget plan",
    '"follow up email"',
    "holiday OR vacation",
    "budget NOT tax",
    "(flight OR train) AND booking",
    "recieve~1",
    "accomodation~2",
    "title:invoice",
    'heading_path:"chapter 4"',
    "author:austen",
    "kind:pdf",
    "path_tokens:taxes",
    "page:[10 TO 20]",
    "garden*",
]


@pytest.mark.parametrize(("doc_input", "expected"), DOCUMENTED_TRANSLATIONS)
def test_documented_translation(doc_input: str, expected: str) -> None:
    assert query_dsl.preprocess(doc_input) == expected


@pytest.mark.parametrize("doc_input", [d for d, _ in DOCUMENTED_TRANSLATIONS] + DOCUMENTED_NATIVE)
def test_documented_examples_parse_in_tantivy(doc_input: str) -> None:
    index = register(tantivy.Index(build_schema()))
    plan = QueryPlan.from_user_text(doc_input)  # must not raise
    # The lexical (filter-stripped) form is what reaches the engine.
    index.parse_query(query_dsl.preprocess(plan.lexical), default_field_names=[F_BODY])
