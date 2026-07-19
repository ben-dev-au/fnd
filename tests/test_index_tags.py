"""Tags reach the index, on every chunk of a file."""

from __future__ import annotations

from pathlib import Path

import tantivy

from fnd.extract.base import Chunk
from fnd.index import _doc_for_chunk
from fnd.schema import F_TAGS_FM, F_TAGS_OS, build_schema


def _chunk(seq: int) -> Chunk:
    return Chunk(
        parent_id="p1",
        path="/tmp/a.md",
        mtime=100,
        kind="md",
        body=f"body {seq}",
        chunk_seq=seq,
    )


def _index_with(tmp_path: Path, docs: list[tantivy.Document]) -> tantivy.Index:
    index = tantivy.Index(build_schema(), path=str(tmp_path))
    writer = index.writer(15_000_000)
    for d in docs:
        writer.add_document(d)
    writer.commit()
    index.reload()
    return index


def _stored(index: tantivy.Index) -> tantivy.Document:
    searcher = index.searcher()
    hits = searcher.search(index.parse_query("body", ["body"]), 5).hits
    return searcher.doc(hits[0][1])


def test_both_sources_land_on_one_document(tmp_path: Path) -> None:
    doc = _doc_for_chunk(
        _chunk(0),
        collection="c",
        tags={"frontmatter": frozenset({"recipe"}), "os": frozenset({"red"})},
    )
    stored = _stored(_index_with(tmp_path, [doc]))
    assert set(stored.get_all(F_TAGS_FM)) == {"recipe"}
    assert set(stored.get_all(F_TAGS_OS)) == {"red"}


def test_tags_repeat_on_every_chunk(tmp_path: Path) -> None:
    """A file's tags must be findable from any of its chunks."""
    tags = {"frontmatter": frozenset({"recipe"})}
    docs = [_doc_for_chunk(_chunk(i), collection="c", tags=tags) for i in range(3)]
    index = _index_with(tmp_path, docs)
    q = tantivy.Query.term_query(build_schema(), F_TAGS_FM, "recipe")
    assert len(index.searcher().search(q, 10).hits) == 3


def test_no_tags_writes_no_values(tmp_path: Path) -> None:
    stored = _stored(_index_with(tmp_path, [_doc_for_chunk(_chunk(0), collection="c", tags=None)]))
    assert stored.get_all(F_TAGS_FM) == []


def test_unknown_provider_id_is_ignored(tmp_path: Path) -> None:
    """A future provider must not crash an older index writer."""
    doc = _doc_for_chunk(
        _chunk(0),
        collection="c",
        tags={"frontmatter": frozenset({"ok"}), "quantum": frozenset({"x"})},
    )
    stored = _stored(_index_with(tmp_path, [doc]))
    assert set(stored.get_all(F_TAGS_FM)) == {"ok"}
