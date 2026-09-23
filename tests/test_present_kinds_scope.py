"""present_kinds must scope by the full active scope — full collections AND the
active sources of partially-selected collections — so the file-type filter never
reveals kinds from unselected sources of the same collection."""

from __future__ import annotations

import tempfile

import tantivy

from fnd.kind_catalogue import present_kinds
from fnd.schema import (
    F_COLLECTION,
    F_KIND,
    F_MEMBERSHIP,
    F_PARENT_ID,
    F_SOURCE_PATH,
    build_schema,
    membership_token,
)


def _index(docs: list[tuple[str, str, str]]) -> tantivy.Index:
    """docs = (collection, source_path, kind)."""
    idx = tantivy.Index(build_schema(), path=tempfile.mkdtemp(prefix="fnd-pk-"))
    w = idx.writer()
    for i, (col, src, kind) in enumerate(docs):
        d = tantivy.Document()
        d.add_text(F_PARENT_ID, f"p{i}")
        d.add_text(F_COLLECTION, col)
        d.add_text(F_SOURCE_PATH, src)
        d.add_text(F_MEMBERSHIP, membership_token(col, src))
        d.add_text(F_KIND, kind)
        w.add_document(d)
    w.commit()
    idx.reload()
    return idx


def test_present_kinds_by_collection_covers_all_its_sources() -> None:
    idx = _index([("A", "/a", "pdf"), ("A", "/b", "cpp"), ("B", "/c", "json")])
    assert present_kinds(idx, collections=["A"]) == {"pdf", "cpp"}


def test_present_kinds_by_source_excludes_unselected_sources() -> None:
    """A partial selection (only source /a of collection A) must NOT reveal the
    cpp kind that only exists in the unselected source /b.

    The scope carries the collection each source came from, so a path listed
    under two collections stays scoped to the one that was ticked.
    """
    idx = _index([("A", "/a", "pdf"), ("A", "/b", "cpp"), ("B", "/c", "json")])
    assert present_kinds(idx, collections=[], source_scope={"A": ["/a"]}) == {"pdf"}
    assert present_kinds(idx, collections=[], source_scope={"A": ["/a", "/b"]}) == {"pdf", "cpp"}


def test_no_scope_sees_everything() -> None:
    """`None` is "there is nothing to scope by"."""
    idx = _index([("A", "/a", "pdf"), ("B", "/c", "json")])
    assert present_kinds(idx, collections=None, source_scope={}) == {"pdf", "json"}


def test_an_explicitly_empty_scope_sees_nothing() -> None:
    """`[]` is "the user unticked everything", and the search returns nothing
    for it. A branch listing every kind beside a `nothing matched` header is
    the disagreement this closes."""
    idx = _index([("A", "/a", "pdf"), ("B", "/c", "json")])
    assert present_kinds(idx, collections=[], source_scope={}) == set()
