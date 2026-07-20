"""Which tags exist in the active collections, and how many files carry each.

Read straight from the index with a terms aggregation rather than kept in a
sidecar, so the list can never drift from what is actually indexed.

Counts are FILE counts, not document counts. A tantivy document is a chunk, so
a 40-chunk PDF tagged ``report`` would otherwise report as 40; a ``cardinality``
sub-aggregation over ``parent_id`` recovers the distinct-file number.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

import tantivy

from fnd.schema import F_COLLECTION, F_PARENT_ID, TAG_FIELD_BY_SOURCE

__all__ = ["TagCount", "TagNode", "build_tag_tree", "tag_catalog"]

# Enough to cover a large vault's tag vocabulary in one pass.
_DEFAULT_LIMIT = 500


@dataclass(slots=True, frozen=True)
class TagCount:
    value: str
    files: int


def _scope_query(index: tantivy.Index, collections: Sequence[str]) -> tantivy.Query:
    """Restrict the aggregation to the active collections (all if empty)."""
    if not collections:
        return tantivy.Query.all_query()
    terms = [tantivy.Query.term_query(index.schema, F_COLLECTION, c) for c in collections]
    if len(terms) == 1:
        return terms[0]
    return tantivy.Query.boolean_query([(tantivy.Occur.Should, t) for t in terms])


def tag_catalog(
    index: tantivy.Index,
    *,
    collections: Sequence[str],
    sources: Sequence[str] | None = None,
    limit: int = _DEFAULT_LIMIT,
) -> dict[str, list[TagCount]]:
    """``{source_id: [TagCount, ...]}`` for the tags present in ``collections``.

    Ordered by file count descending, then name, so the pane can show the
    most-used tags first. ``sources`` defaults to every known provider.

    A failed aggregation yields an empty list for that source rather than
    raising: an unreadable catalogue must not take the filters pane down.
    """
    wanted = list(sources) if sources is not None else list(TAG_FIELD_BY_SOURCE)
    scope = _scope_query(index, collections)
    searcher = index.searcher()

    out: dict[str, list[TagCount]] = {}
    for source in wanted:
        field_name = TAG_FIELD_BY_SOURCE.get(source)
        if field_name is None:
            continue
        agg = {
            "tags": {
                "terms": {"field": field_name, "size": limit},
                "aggs": {"files": {"cardinality": {"field": F_PARENT_ID}}},
            }
        }
        try:
            raw = searcher.aggregate(scope, agg)
            buckets = raw["tags"]["buckets"]
        except Exception:
            out[source] = []
            continue
        # cardinality is HyperLogLog, so the value arrives as a float; it is
        # exact at realistic tag sizes and drifts ~1% only across thousands
        # of files.
        counts = [TagCount(value=str(b["key"]), files=round(b["files"]["value"])) for b in buckets]
        # Tantivy orders by doc (chunk) count; re-sort on the file count we
        # actually display, so the pane's order matches its numbers.
        counts.sort(key=lambda t: (-t.files, t.value))
        out[source] = counts[:limit]
    return out


@dataclass(slots=True)
class TagNode:
    """One row of the Tags pane.

    ``label`` is the leaf segment shown to the user; ``value`` is the full
    ``a/b/c`` path the filter must actually use.
    """

    label: str
    value: str
    files: int = 0
    children: list[TagNode] = field(default_factory=list)

    def descendant_values(self) -> set[str]:
        """This node's value plus every value beneath it."""
        out = {self.value}
        for child in self.children:
            out |= child.descendant_values()
        return out


def build_tag_tree(counts: Sequence[TagCount]) -> list[TagNode]:
    """Nest ``a/b`` tags under ``a``, ordered by file count then name.

    Ancestors are normally expanded at index time so every parent is itself a
    real catalogue entry, but a catalogue truncated by ``limit`` can drop one;
    a missing ancestor is synthesised with a zero count so its children are
    still reachable rather than silently lost.
    """
    roots: dict[str, TagNode] = {}
    by_value: dict[str, TagNode] = {}

    for entry in sorted(counts, key=lambda t: t.value):
        parts = [p for p in entry.value.split("/") if p]
        if not parts:
            continue
        parent: TagNode | None = None
        for depth in range(len(parts)):
            path = "/".join(parts[: depth + 1])
            node = by_value.get(path)
            if node is None:
                node = TagNode(label=parts[depth], value=path)
                by_value[path] = node
                if parent is None:
                    roots[path] = node
                else:
                    parent.children.append(node)
            parent = node
        # parent is non-None here: the loop runs at least once because
        # `parts` is non-empty, but pyright can't see that.
        if parent is not None:
            parent.files = entry.files

    def order(nodes: list[TagNode]) -> list[TagNode]:
        for n in nodes:
            n.children = order(n.children)
        return sorted(nodes, key=lambda n: (-n.files, n.label))

    return order(list(roots.values()))
