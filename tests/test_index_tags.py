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


def test_build_index_writes_tags(tmp_path: Path) -> None:
    """`fnd index <root>` uses build_index, not the config path. Tags must
    land there too, or an ad-hoc index is silently untaggable."""
    from fnd.index import build_index
    from fnd.query import Searcher
    from fnd.tag_query import TagFilter

    root = tmp_path / "corpus"
    root.mkdir()
    (root / "a.md").write_text(
        "---\ntags: [recipe, project/alpha]\n---\n\n# A\n\nsaffron\n", encoding="utf-8"
    )
    (root / "b.md").write_text("# B\n\nsaffron plain\n", encoding="utf-8")

    index_dir = tmp_path / "idx"
    build_index(roots=[root], index_dir=index_dir, collection="default")

    searcher = Searcher(index_dir=index_dir)
    hits = searcher.search(
        "saffron", tag_filter=TagFilter(include={"frontmatter": frozenset({"recipe"})})
    )
    assert {Path(h.path).name for h in hits} == {"a.md"}


def test_build_index_expands_nested_tags(tmp_path: Path) -> None:
    from fnd.index import build_index
    from fnd.query import Searcher
    from fnd.tag_query import TagFilter

    root = tmp_path / "corpus"
    root.mkdir()
    (root / "a.md").write_text(
        "---\ntags: [project/alpha]\n---\n\n# A\n\nsaffron\n", encoding="utf-8"
    )
    index_dir = tmp_path / "idx"
    build_index(roots=[root], index_dir=index_dir, collection="default")

    searcher = Searcher(index_dir=index_dir)
    hits = searcher.search(
        "saffron", tag_filter=TagFilter(include={"frontmatter": frozenset({"project"})})
    )
    assert {Path(h.path).name for h in hits} == {"a.md"}
