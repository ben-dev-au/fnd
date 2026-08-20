"""Lock the README §"Search how-to" examples to actual behaviour: every
documented query expands to the documented form and parses in Tantivy without
error. Keeps the docs and the engine from drifting apart."""

from __future__ import annotations

import pytest
import tantivy

from fnd import query_dsl
from fnd.query_plan import QueryPlan
from fnd.schema import F_BODY, build_schema

# (documented input, expected DSL translation). Time-relative forms (mtime
# tokens / ISO compares) are covered separately in test_query_dsl.
DOCUMENTED_TRANSLATIONS = [
    ("{5} cross entropy", '"cross entropy"~5'),
    ("cross NEAR/5 entropy", '"cross entropy"~5'),
    ("{20} man in the middle attack", '"man in the middle attack"~20'),
    ("{60} buffer overflow exploit", '"buffer overflow exploit"~60'),
    ("{500} race condition mitigations", '"race condition mitigations"~500'),
    # Worked example (README composing section): proximity stops at the qualifier.
    ("{10} buffer overflow exploit kind:pdf", '"buffer overflow exploit"~10 kind:pdf'),
    ("c:wine attack", 'collection:"wine" attack'),
    ("c:notes,papers transformer", '(collection:"notes" OR collection:"papers") transformer'),
    ("page:>20", f"page:[21 TO {query_dsl.FAR_FUTURE}]"),
    ("slide:<5", f"slide:[{query_dsl.FAR_PAST} TO 4]"),
]

# Documented inputs Tantivy/our DSL pass through unchanged.
DOCUMENTED_NATIVE = [
    "entropy",
    "cross entropy loss",
    '"cross entropy loss"',
    "cross OR entropy",
    "entropy NOT regression",
    "(loss OR cost) AND function",
    "mitochondira~1",
    "kubernates~2",
    "title:transformer",
    'heading_path:"chapter 4"',
    "author:dijkstra",
    "kind:pdf",
    "path_tokens:thesis",
    "page:[10 TO 20]",
    "crypto*",
]


@pytest.mark.parametrize(("doc_input", "expected"), DOCUMENTED_TRANSLATIONS)
def test_documented_translation(doc_input: str, expected: str) -> None:
    assert query_dsl.preprocess(doc_input) == expected


@pytest.mark.parametrize("doc_input", [d for d, _ in DOCUMENTED_TRANSLATIONS] + DOCUMENTED_NATIVE)
def test_documented_examples_parse_in_tantivy(doc_input: str) -> None:
    index = tantivy.Index(build_schema())
    plan = QueryPlan.from_user_text(doc_input)  # must not raise
    # The lexical (filter-stripped) form is what reaches the engine.
    index.parse_query(query_dsl.preprocess(plan.lexical), default_field_names=[F_BODY])
