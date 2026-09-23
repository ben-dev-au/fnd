"""Which file-type kinds exist in the active collections.

Read from the index with a terms aggregation over ``F_KIND`` — the same live
mechanism as :mod:`fnd.tag_catalogue` — so the file-type filter shows only the
kinds actually present and never drifts from what is indexed.

``F_KIND`` is ``fast=True, tokenizer_name="raw"`` (see :mod:`fnd.schema`), which
is exactly what a terms aggregation needs.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import tantivy

from fnd.schema import F_KIND

__all__ = ["present_kinds"]

# Well above the registry's kind count; one bucket per distinct kind present.
_MAX_KINDS = 256


def _scope_query(
    index: tantivy.Index,
    collections: Sequence[str] | None,
    source_scope: Mapping[str, Sequence[str]] | None,
) -> tantivy.Query:
    """Restrict the aggregation to the active scope, built by the same
    :func:`fnd.query.scope_arms` the search uses so the facets cannot offer a
    kind the results exclude.

    ``collections=None`` means unscoped; an empty list means the user unticked
    everything, which matches NOTHING. Collapsing those two listed every kind
    in the index beside a `nothing matched` header."""
    from fnd.query import scope_arms, scope_or

    arms = scope_arms(
        index.schema, None if collections is None else list(collections), source_scope
    )
    if arms is None:
        return tantivy.Query.all_query()
    if not arms:
        return tantivy.Query.empty_query()
    return scope_or(arms)


def present_kinds(
    index: tantivy.Index,
    *,
    collections: Sequence[str] | None,
    source_scope: Mapping[str, Sequence[str]] | None = None,
) -> set[str] | None:
    """Kind ids present in the active scope (whole index if scope is empty).

    ``collections`` are fully-selected collections; ``source_scope`` maps a
    partly-selected collection to its own ticked sources. Same shape and same
    builder as the search's scope, so the file-type filter can never offer a
    kind the results exclude.

    Returns ``None`` when the aggregation cannot be computed, so the caller can
    fall back to showing all kinds rather than an empty filter. An empty set
    means the scope genuinely contains no indexed files.
    """
    agg: dict[str, object] = {"kinds": {"terms": {"field": F_KIND, "size": _MAX_KINDS}}}
    try:
        raw = index.searcher().aggregate(_scope_query(index, collections, source_scope), agg)
        return {str(bucket["key"]) for bucket in raw["kinds"]["buckets"]}
    except Exception:
        return None
