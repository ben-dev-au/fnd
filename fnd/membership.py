"""Cross-collection membership set algebra.

A file is stored once in the index; the collections it belongs to are a set of
``(collection, source_root)`` pairs on that single document. These pure helpers
are the one place the membership is changed, shared by the sync and async
indexers so the two can never diverge on the rule.
"""

from __future__ import annotations

Pair = tuple[str, str]  #: (collection, source_root)


def after_index(prior: frozenset[Pair], collection: str, source: str) -> frozenset[Pair]:
    """Membership after ``collection`` reaches the file via ``source``.

    A collection owns one source per file (first-source-wins in the walk), so
    this replaces any existing pair for ``collection`` rather than accumulating.
    """
    return frozenset({p for p in prior if p[0] != collection} | {(collection, source)})


def after_prune(prior: frozenset[Pair], collection: str) -> frozenset[Pair]:
    """Membership after ``collection`` no longer reaches the file. An empty
    result means the file belongs to no collection and its document should be
    deleted, not rewritten."""
    return frozenset(p for p in prior if p[0] != collection)


def collections_of(membership: frozenset[Pair]) -> set[str]:
    """The distinct collections in a membership set."""
    return {collection for collection, _ in membership}
