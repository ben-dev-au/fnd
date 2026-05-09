"""Tantivy IndexWriter wrapper + ``build_index`` entry point.

Phase 1: single-process, single-writer. Phase 7 adds reranker; phase 10 adds
fsevents incremental updates and the long-running watcher.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from pathlib import Path

from tantivy import Document, Index

from acorn.config import CollectionConfig
from acorn.extract import Chunk, extract
from acorn.meta_blob import encode as encode_meta_blob
from acorn.schema import (
    F_AUTHOR,
    F_BODY,
    F_BODY_STRUCT,
    F_CHUNK_SEQ,
    F_COLLECTION,
    F_HEADING_PATH,
    F_KIND,
    F_META_BLOB,
    F_MTIME,
    F_PAGE,
    F_PARENT_ID,
    F_PATH,
    F_PATH_TOKENS,
    F_SLIDE,
    F_TITLE,
    SCHEMA_VERSION,
    build_schema,
)
from acorn.struct import encode as encode_body_struct
from acorn.walk import walk

# 50 MB heap for the writer; tune later if 50k corpus is sluggish.
_WRITER_HEAP = 50_000_000

# Commit every N chunks so partial-progress is queryable mid-index.
_COMMIT_BATCH = 500


def _ensure_index(index_dir: Path, *, force: bool = False) -> Index:
    index_dir = index_dir.expanduser().resolve()
    index_dir.mkdir(parents=True, exist_ok=True)
    schema = build_schema()
    # Index.open_or_create returns an existing index if the schema matches; the
    # SCHEMA_VERSION sidecar guards against silent format changes.
    sidecar = index_dir / ".acorn-schema-version"
    if sidecar.exists():
        existing = sidecar.read_text().strip()
        if existing != str(SCHEMA_VERSION):
            if not force:
                raise RuntimeError(
                    f"index at {index_dir} has schema version {existing}; current is "
                    f"{SCHEMA_VERSION}. Rebuild with --rebuild."
                )
            # Rebuild path: overwrite the sidecar to the current version now.
            sidecar.write_text(str(SCHEMA_VERSION))
    else:
        sidecar.write_text(str(SCHEMA_VERSION))
    return Index(schema, path=str(index_dir))


def _doc_for_chunk(chunk: Chunk, *, collection: str, meta_blob_bytes: bytes = b"") -> Document:
    doc = Document()
    doc.add_text(F_PARENT_ID, chunk.parent_id)
    doc.add_text(F_COLLECTION, collection)
    doc.add_text(F_PATH, chunk.path)
    doc.add_text(F_PATH_TOKENS, chunk.path)
    doc.add_text(F_KIND, chunk.kind)
    doc.add_text(F_HEADING_PATH, chunk.heading_path)
    doc.add_text(F_TITLE, chunk.title)
    doc.add_text(F_AUTHOR, chunk.author)
    doc.add_text(F_BODY, chunk.body)
    doc.add_unsigned(F_MTIME, max(chunk.mtime, 0))
    doc.add_unsigned(F_PAGE, max(chunk.page, 0))
    doc.add_unsigned(F_SLIDE, max(chunk.slide, 0))
    doc.add_unsigned(F_CHUNK_SEQ, max(chunk.chunk_seq, 0))
    doc.add_bytes(F_BODY_STRUCT, encode_body_struct(chunk.body_struct))
    doc.add_bytes(F_META_BLOB, meta_blob_bytes)
    return doc


def build_index(
    *,
    roots: Sequence[Path],
    index_dir: Path,
    collection: str = "default",
    includes: list[str] | None = None,
    excludes: list[str] | None = None,
    follow_symlinks: bool = False,
    rebuild: bool = False,
) -> int:
    """Index supported files under ``roots`` into ``index_dir``.

    Honours includes/excludes globs per §8 precedence rules. Returns the number
    of chunks written. Phase 1 is single-process and single-writer; multi-
    process extraction lands in phase 10.

    When ``rebuild=True``, all existing chunks for ``collection`` are deleted
    before re-adding — useful when an extractor improves and the user wants
    fresh chunks without losing other collections.
    """
    index = _ensure_index(index_dir, force=rebuild)
    writer = index.writer(heap_size=_WRITER_HEAP)

    if rebuild:
        writer.delete_documents(F_COLLECTION, collection)
        writer.commit()

    written = 0
    paths: Iterable[Path] = walk(
        roots=roots,
        includes=includes,
        excludes=excludes,
        follow_symlinks=follow_symlinks,
    )
    for path in paths:
        # Idempotent re-index: delete chunks for this file then re-add. Phase
        # 10 adds mtime gating to skip unchanged files entirely.
        writer.delete_documents(F_PARENT_ID, _path_parent_id(path))
        for chunk in extract(path):
            writer.add_document(_doc_for_chunk(chunk, collection=collection))
            written += 1
            if written % _COMMIT_BATCH == 0:
                writer.commit()
    writer.commit()
    writer.wait_merging_threads()
    return written


def build_index_from_config(
    *,
    config: CollectionConfig,
    collection: str,
    index_dir: Path,
    rebuild: bool = False,
) -> int:
    """Build a collection from its :class:`CollectionConfig`.

    Walks each source's filter chain via :func:`acorn.walk.walk_sources`
    and indexes the surviving paths. The legacy flat-shape config is
    auto-promoted to a single implicit source by the loader, so this
    function only sees the new shape. For md files, frontmatter is read
    once per file and serialized into ``meta_blob`` on every chunk so the
    query-time post-filter (§5.5e-2) can decode + evaluate it.
    """
    from acorn.frontmatter import (
        FrontmatterParseError,
        read_frontmatter_from_file,
    )
    from acorn.walk import walk_sources

    index = _ensure_index(index_dir, force=rebuild)
    writer = index.writer(heap_size=_WRITER_HEAP)
    if rebuild:
        writer.delete_documents(F_COLLECTION, collection)
        writer.commit()
    written = 0
    for path in walk_sources(sources=config.sources):
        meta_blob_bytes = b""
        if path.suffix.lower() == ".md":
            try:
                fm = read_frontmatter_from_file(path)
            except FrontmatterParseError:
                fm = None
            if fm:
                meta_blob_bytes = encode_meta_blob(fm)
        writer.delete_documents(F_PARENT_ID, _path_parent_id(path))
        for chunk in extract(path):
            writer.add_document(
                _doc_for_chunk(chunk, collection=collection, meta_blob_bytes=meta_blob_bytes)
            )
            written += 1
            if written % _COMMIT_BATCH == 0:
                writer.commit()
    writer.commit()
    writer.wait_merging_threads()
    return written


def _path_parent_id(path: Path) -> str:
    """Mirror of the extractor's hashing so deletes target the right docs."""
    import hashlib

    return hashlib.sha1(str(path.resolve()).encode("utf-8")).hexdigest()
