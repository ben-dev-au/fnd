"""Which file-type kinds exist in the active collections.

Read from the index with a terms aggregation over ``F_KIND`` — the same live
mechanism as :mod:`fnd.tag_catalog` — so the file-type filter shows only the
kinds actually present and never drifts from what is indexed.

``F_KIND`` is ``fast=True, tokenizer_name="raw"`` (see :mod:`fnd.schema`), which
is exactly what a terms aggregation needs.
"""

from __future__ import annotations

from collections.abc import Sequence

import tantivy

from fnd.schema import F_COLLECTION, F_KIND

__all__ = ["present_kinds"]

# Well above the registry's kind count; one bucket per distinct kind present.
_MAX_KINDS = 256


def _scope_query(
    index: tantivy.Index,
    collections: Sequence[str],
    query: tantivy.Query | None = None,
) -> tantivy.Query:
    """Restrict the aggregation to the active collections (all if empty) and to
    ``query`` when a search is active. Mirrors ``tag_catalog._scope_query``."""
    clauses: list[tuple[tantivy.Occur, tantivy.Query]] = []
    if collections:
        terms = [tantivy.Query.term_query(index.schema, F_COLLECTION, c) for c in collections]
        col_q = (
            terms[0]
            if len(terms) == 1
            else tantivy.Query.boolean_query([(tantivy.Occur.Should, t) for t in terms])
        )
        clauses.append((tantivy.Occur.Must, col_q))
    if query is not None:
        clauses.append((tantivy.Occur.Must, query))
    if not clauses:
        return tantivy.Query.all_query()
    if len(clauses) == 1:
        return clauses[0][1]
    return tantivy.Query.boolean_query(clauses)


def present_kinds(
    index: tantivy.Index,
    *,
    collections: Sequence[str],
    query: tantivy.Query | None = None,
) -> set[str] | None:
    """Kind ids present in ``collections`` (every collection if empty).

    Returns ``None`` when the aggregation cannot be computed, so the caller can
    fall back to showing all kinds rather than an empty filter. An empty set
    means the scope genuinely contains no indexed files.
    """
    agg: dict[str, object] = {"kinds": {"terms": {"field": F_KIND, "size": _MAX_KINDS}}}
    try:
        raw = index.searcher().aggregate(_scope_query(index, collections, query), agg)
        return {str(bucket["key"]) for bucket in raw["kinds"]["buckets"]}
    except Exception:
        return None
