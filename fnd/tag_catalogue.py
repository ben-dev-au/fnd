"""Which tags exist in the active collections, and how many files carry each.

Read straight from the index with a terms aggregation rather than kept in a
sidecar, so the list can never drift from what is actually indexed.

Counts are FILE counts, not document counts. A tantivy document is a chunk, so
a 40-chunk PDF tagged ``report`` would otherwise report as 40. The aggregation
buckets by file and tallies tags within each, which is exact — see the note in
:func:`tag_catalogue` for why the cardinality approach was abandoned.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field

import tantivy

from fnd.schema import F_COLLECTION, F_PARENT_ID, TAG_FIELD_BY_SOURCE

__all__ = ["TagCount", "TagNode", "build_tag_tree", "tag_catalogue"]

# Enough to cover a large vault's tag vocabulary in one pass.
_DEFAULT_LIMIT = 500
# The catalogue buckets by file, so this caps how many FILES are inspected.
# Beyond it a tag on only the excess files would be missed; sized well above
# any realistic single-collection file count.
_MAX_FILE_BUCKETS = 200_000
# Per-file tag cap, matching fnd.tags.MAX_TAGS_PER_FILE.
_MAX_TAGS_PER_FILE = 256


@dataclass(slots=True, frozen=True)
class TagCount:
    value: str
    files: int


def _scope_query(
    index: tantivy.Index,
    collections: Sequence[str],
    query: tantivy.Query | None = None,
) -> tantivy.Query:
    """Restrict the aggregation to the active collections, and to ``query``
    when a search is active. Empty collections means every collection."""
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


def tag_catalogue(
    index: tantivy.Index,
    *,
    collections: Sequence[str],
    sources: Sequence[str] | None = None,
    query: tantivy.Query | None = None,
    limit: int = _DEFAULT_LIMIT,
) -> dict[str, list[TagCount]]:
    """``{source_id: [TagCount, ...]}`` for the tags present in ``collections``.

    Ordered by file count descending, then name, so the pane can show the
    most-used tags first. ``sources`` defaults to every known provider.

    ``query`` narrows the catalogue to the files matching the active search, so
    counts describe what the user is actually looking at. It must NOT include
    the tag filter itself: computing facets over their own filter is what makes
    sibling tags vanish the moment one is selected.

    A failed aggregation yields an empty list for that source rather than
    raising: an unreadable catalogue must not take the filters pane down.
    """
    wanted = [
        s
        for s in (sources if sources is not None else TAG_FIELD_BY_SOURCE)
        if s in TAG_FIELD_BY_SOURCE
    ]
    out: dict[str, list[TagCount]] = {s: [] for s in wanted}
    if not wanted:
        return out

    # Bucket by FILE, then by tag within each file, and count the file buckets
    # each tag appears in. The obvious shape (bucket by tag, cardinality over
    # parent_id) silently returns 0 for some buckets — measured on a real
    # corpus, `exam` had 34 chunks and a cardinality of 0.0 — which would hide
    # real tags behind a "(0)" count. This inversion is exact and, measured on
    # the same corpus, faster.
    agg: dict[str, object] = {
        "files": {
            "terms": {"field": F_PARENT_ID, "size": _MAX_FILE_BUCKETS},
            "aggs": {
                source: {
                    "terms": {"field": TAG_FIELD_BY_SOURCE[source], "size": _MAX_TAGS_PER_FILE}
                }
                for source in wanted
            },
        }
    }
    try:
        raw = index.searcher().aggregate(_scope_query(index, collections, query), agg)
        file_buckets = raw["files"]["buckets"]
    except Exception:
        # An unreadable catalogue must not take the filters pane down.
        return out

    tallies: dict[str, Counter[str]] = {s: Counter() for s in wanted}
    for file_bucket in file_buckets:
        for source in wanted:
            sub = file_bucket.get(source)
            if not sub:
                continue
            for tag_bucket in sub["buckets"]:
                tallies[source][str(tag_bucket["key"])] += 1

    for source, tally in tallies.items():
        counts = [TagCount(value=v, files=n) for v, n in tally.items()]
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
