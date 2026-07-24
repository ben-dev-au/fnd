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

from fnd.schema import F_COLLECTION, F_KIND, F_SOURCE_PATH

__all__ = ["present_kinds"]

# Well above the registry's kind count; one bucket per distinct kind present.
_MAX_KINDS = 256


def _or_terms(index: tantivy.Index, field: str, values: Sequence[str]) -> tantivy.Query:
    terms = [tantivy.Query.term_query(index.schema, field, v) for v in values]
    if len(terms) == 1:
        return terms[0]
    return tantivy.Query.boolean_query([(tantivy.Occur.Should, t) for t in terms])


def _scope_query(
    index: tantivy.Index,
    collections: Sequence[str],
    source_paths: Sequence[str],
) -> tantivy.Query:
    """Restrict the aggregation to the active scope — full collections (by
    ``F_COLLECTION``) plus the active sources of partially-selected collections
    (by ``F_SOURCE_PATH``), ANDed together exactly like the search's hard scope
    filters (see ``query._raw_hits``). Empty scope aggregates the whole index."""
    clauses: list[tuple[tantivy.Occur, tantivy.Query]] = []
    if collections:
        clauses.append((tantivy.Occur.Must, _or_terms(index, F_COLLECTION, collections)))
    if source_paths:
        clauses.append((tantivy.Occur.Must, _or_terms(index, F_SOURCE_PATH, source_paths)))
    if not clauses:
        return tantivy.Query.all_query()
    if len(clauses) == 1:
        return clauses[0][1]
    return tantivy.Query.boolean_query(clauses)


def present_kinds(
    index: tantivy.Index,
    *,
    collections: Sequence[str],
    source_paths: Sequence[str] = (),
) -> set[str] | None:
    """Kind ids present in the active scope (whole index if scope is empty).

    ``collections`` are fully-selected collections; ``source_paths`` are the
    active sources of partially-selected collections — matching the search's
    scope so the file-type filter never reveals kinds from unselected sources.

    Returns ``None`` when the aggregation cannot be computed, so the caller can
    fall back to showing all kinds rather than an empty filter. An empty set
    means the scope genuinely contains no indexed files.
    """
    agg: dict[str, object] = {"kinds": {"terms": {"field": F_KIND, "size": _MAX_KINDS}}}
    try:
        raw = index.searcher().aggregate(_scope_query(index, collections, source_paths), agg)
        return {str(bucket["key"]) for bucket in raw["kinds"]["buckets"]}
    except Exception:
        return None
