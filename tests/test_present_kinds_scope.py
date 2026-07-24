"""present_kinds must scope by the full active scope — full collections AND the
active sources of partially-selected collections — so the file-type filter never
reveals kinds from unselected sources of the same collection."""

from __future__ import annotations

import tempfile

import tantivy

from fnd.kind_catalog import present_kinds
from fnd.schema import F_COLLECTION, F_KIND, F_PARENT_ID, F_SOURCE_PATH, build_schema


def _index(docs: list[tuple[str, str, str]]) -> tantivy.Index:
    """docs = (collection, source_path, kind)."""
    idx = tantivy.Index(build_schema(), path=tempfile.mkdtemp(prefix="fnd-pk-"))
    w = idx.writer()
    for i, (col, src, kind) in enumerate(docs):
        d = tantivy.Document()
        d.add_text(F_PARENT_ID, f"p{i}")
        d.add_text(F_COLLECTION, col)
        d.add_text(F_SOURCE_PATH, src)
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
    cpp kind that only exists in the unselected source /b."""
    idx = _index([("A", "/a", "pdf"), ("A", "/b", "cpp"), ("B", "/c", "json")])
    assert present_kinds(idx, collections=[], source_paths=["/a"]) == {"pdf"}
    assert present_kinds(idx, collections=[], source_paths=["/a", "/b"]) == {"pdf", "cpp"}


def test_present_kinds_empty_scope_sees_everything() -> None:
    idx = _index([("A", "/a", "pdf"), ("B", "/c", "json")])
    assert present_kinds(idx, collections=[], source_paths=[]) == {"pdf", "json"}
