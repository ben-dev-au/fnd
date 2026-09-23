"""The pure core of cross-collection storage: how a file's membership changes.

A file is stored once; its membership is a set of (collection, source) pairs.
Indexing adds or refreshes one collection's pair; pruning removes it; an empty
result means the file belongs to nothing and its document should be deleted.
"""

from __future__ import annotations

from fnd.membership import after_index, after_prune, collections_of


def test_indexing_a_new_file_records_one_pair() -> None:
    assert after_index(frozenset(), "C1", "S1") == frozenset({("C1", "S1")})


def test_a_second_collection_joins_rather_than_replaces() -> None:
    prior = frozenset({("C1", "S1")})
    assert after_index(prior, "C2", "S2") == frozenset({("C1", "S1"), ("C2", "S2")})


def test_reindexing_a_collection_replaces_its_own_source() -> None:
    """The control that bites: a naive union would leave two sources for one
    collection. A collection owns exactly one source per file."""
    prior = frozenset({("C1", "S1")})
    assert after_index(prior, "C1", "S2") == frozenset({("C1", "S2")})


def test_prune_removes_only_the_named_collection() -> None:
    prior = frozenset({("C1", "S1"), ("C2", "S2")})
    assert after_prune(prior, "C1") == frozenset({("C2", "S2")})


def test_prune_to_empty_signals_the_document_should_go() -> None:
    assert after_prune(frozenset({("C1", "S1")}), "C1") == frozenset()


def test_collections_of_deduplicates_across_sources() -> None:
    m = frozenset({("C1", "S1"), ("C1", "Salt"), ("C2", "S2")})
    assert collections_of(m) == {"C1", "C2"}
