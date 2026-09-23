"""One document, many collection memberships, and source-scope that stays exact.

A file shared across collections is stored once. Whole-collection scope reads
F_COLLECTION (multi-valued); source scope reads a compound (collection, source)
token, because the pairing is what a public corpus needs and separate multi
fields lose it: a file in C1-via-S1 and C2-via-S2 must NOT answer to C1-via-S2.
"""

from __future__ import annotations

from tantivy import Index, Occur, Query, Schema

from fnd.schema import (
    F_COLLECTION,
    F_MEMBERSHIP,
    F_PARENT_ID,
    F_SOURCE_PATH,
    build_schema,
    membership_token,
)


def _index_with_one_shared_doc() -> tuple[Index, Schema]:
    """One document: in C1 via source S1, and in C2 via source S2."""
    schema = build_schema()
    index = Index(schema)
    writer = index.writer()
    from tantivy import Document

    doc = Document()
    doc.add_text(F_PARENT_ID, "shared")
    doc.add_text(F_COLLECTION, "C1")
    doc.add_text(F_COLLECTION, "C2")
    doc.add_text(F_SOURCE_PATH, "S1")
    doc.add_text(F_SOURCE_PATH, "S2")
    doc.add_text(F_MEMBERSHIP, membership_token("C1", "S1"))
    doc.add_text(F_MEMBERSHIP, membership_token("C2", "S2"))
    writer.add_document(doc)
    writer.commit()
    index.reload()
    return index, schema


def _n(index: Index, query: Query) -> int:
    return len(index.searcher().search(query, limit=10).hits)


def test_whole_collection_scope_matches_either_membership() -> None:
    index, schema = _index_with_one_shared_doc()

    assert _n(index, Query.term_query(schema, F_COLLECTION, "C1")) == 1
    assert _n(index, Query.term_query(schema, F_COLLECTION, "C2")) == 1
    assert _n(index, Query.term_query(schema, F_COLLECTION, "C3")) == 0


def test_source_scope_matches_the_exact_pair_only() -> None:
    """The general-case correctness: the real pairs match, the cross pair does not."""
    index, schema = _index_with_one_shared_doc()

    assert _n(index, Query.term_query(schema, F_MEMBERSHIP, membership_token("C1", "S1"))) == 1
    assert _n(index, Query.term_query(schema, F_MEMBERSHIP, membership_token("C2", "S2"))) == 1
    # The file is NOT in C1 via S2, so C1-scoped-to-S2 must find nothing.
    assert _n(index, Query.term_query(schema, F_MEMBERSHIP, membership_token("C1", "S2"))) == 0


def test_separate_fields_would_mis_pair_which_is_why_the_compound_exists() -> None:
    """The control that bites: matching collection AND source as separate terms
    falsely pairs C1 with S2. If a future change dropped the compound and scoped
    on the two fields, this false match is what it would ship."""
    index, schema = _index_with_one_shared_doc()

    separate = Query.boolean_query(
        [
            (Occur.Must, Query.term_query(schema, F_COLLECTION, "C1")),
            (Occur.Must, Query.term_query(schema, F_SOURCE_PATH, "S2")),
        ]
    )
    assert _n(index, separate) == 1, "separate fields mis-pair C1 with S2"
    compound = Query.term_query(schema, F_MEMBERSHIP, membership_token("C1", "S2"))
    assert _n(index, compound) == 0, "the compound keeps the pairing exact"


def test_read_membership_recovers_every_pair() -> None:
    from fnd.index import read_membership

    index, schema = _index_with_one_shared_doc()
    got = read_membership(index.searcher(), schema, "shared")
    assert got == frozenset({("C1", "S1"), ("C2", "S2")})


def test_read_membership_of_an_absent_file_is_empty() -> None:
    from fnd.index import read_membership

    index, schema = _index_with_one_shared_doc()
    assert read_membership(index.searcher(), schema, "not-in-index") == frozenset()
