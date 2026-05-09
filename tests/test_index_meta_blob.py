"""Phase 5.5e-2: index pipeline serializes frontmatter into meta_blob.

Reads meta_blob via the Tantivy doc store directly because Hit doesn't
carry the field until Task 5; once Task 5 lands, this could simplify to
``hit.meta_blob`` but we keep the doc-store path for stability.
"""

from __future__ import annotations

from pathlib import Path

from acorn.config import CollectionConfig, SourceConfig
from acorn.index import build_index_from_config
from acorn.meta_blob import decode
from acorn.schema import F_META_BLOB, build_schema


def _touch(p: Path, body: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")


def _meta_blob_for_first_hit(index_dir: Path, query: str) -> bytes:
    """Pull the first match for ``query`` and return its meta_blob bytes
    via the doc-store API."""
    from tantivy import Index

    index = Index(build_schema(), path=str(index_dir))
    index.reload()
    searcher = index.searcher()
    parsed = index.parse_query(query, default_field_names=["body"])
    result = searcher.search(parsed, limit=1)
    if not result.hits:
        return b""
    _score, address = result.hits[0]
    doc = searcher.doc(address)
    val = doc.get_first(F_META_BLOB)  # type: ignore[attr-defined]
    return val if val is not None else b""


def test_md_chunk_carries_frontmatter_in_meta_blob(tmp_path: Path, tmp_index_dir: Path) -> None:
    notes = tmp_path / "notes"
    _touch(
        notes / "a.md",
        "---\nCourse: DPwC\ntags: [course, active]\n---\n# A\nbody one\n",
    )
    cc = CollectionConfig(sources=[SourceConfig(path=notes, includes=["**/*.md"])])
    build_index_from_config(config=cc, collection="x", index_dir=tmp_index_dir)
    blob = _meta_blob_for_first_hit(tmp_index_dir, "body")
    assert decode(blob) == {"Course": "DPwC", "tags": ["course", "active"]}


def test_non_md_chunk_meta_blob_is_empty(tmp_path: Path, tmp_index_dir: Path) -> None:
    """Only md files have YAML frontmatter; non-md chunks store empty
    bytes so query-time filters can short-circuit cheaply."""
    root = tmp_path / "txt"
    _touch(root / "a.txt", "this is plain text with no frontmatter")
    cc = CollectionConfig(sources=[SourceConfig(path=root, includes=["**/*.txt"])])
    build_index_from_config(config=cc, collection="x", index_dir=tmp_index_dir)
    blob = _meta_blob_for_first_hit(tmp_index_dir, "plain")
    assert blob == b""


def test_md_without_frontmatter_meta_blob_is_empty(tmp_path: Path, tmp_index_dir: Path) -> None:
    notes = tmp_path / "notes"
    _touch(notes / "a.md", "# Heading\nplain markdown body\n")
    cc = CollectionConfig(sources=[SourceConfig(path=notes, includes=["**/*.md"])])
    build_index_from_config(config=cc, collection="x", index_dir=tmp_index_dir)
    blob = _meta_blob_for_first_hit(tmp_index_dir, "plain")
    assert blob == b""
