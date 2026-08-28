"""Tantivy IndexWriter wrapper + ``build_index`` entry point.

Phase 1: single-process, single-writer. Phase 7 adds reranker; phase 10 adds
fsevents incremental updates and the long-running watcher.
"""

from __future__ import annotations

import datetime as _dt
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path

from tantivy import Document, Index, IndexWriter, Query, Schema

from fnd.config import CollectionConfig
from fnd.extract import Chunk, ExtractError, extract
from fnd.meta_blob import encode as encode_meta_blob
from fnd.schema import (
    F_AUTHOR,
    F_BODY,
    F_BODY_MD,
    F_BODY_STRUCT,
    F_CHUNK_SEQ,
    F_COLLECTION,
    F_CREATED,
    F_HEADING_PATH,
    F_INODE_CTIME,
    F_KIND,
    F_LINE,
    F_META_BLOB,
    F_MTIME,
    F_PAGE,
    F_PAGE_LABEL,
    F_PARENT_ID,
    F_PATH,
    F_PATH_TOKENS,
    F_SLIDE,
    F_SOURCE_PATH,
    F_TITLE,
    SCHEMA_VERSION,
    TAG_FIELD_BY_SOURCE,
    build_schema,
)
from fnd.struct import encode as encode_body_struct
from fnd.walk import walk

# 50 MB heap for the writer; tune later if 50k corpus is sluggish.
_WRITER_HEAP = 50_000_000

# Commit every N chunks so partial-progress is queryable mid-index.
_COMMIT_BATCH = 500

# Backoff before re-attempting a commit Windows refused. Totals 3.15s across
# six waits, which covers a scanner's hold on a file it has just seen.
_COMMIT_RETRY_DELAYS: tuple[float, ...] = (0.05, 0.1, 0.2, 0.4, 0.8, 1.6)


def _commit_is_retryable(exc: BaseException) -> bool:
    """Windows' refusal to replace a file another handle has open.

    The prefix is what makes this safe, not the Win32 text: a store-write
    refusal (``Failed to open file for write:``) prints the same
    ``Access is denied.`` and leaves the writer DEAD — measured, every later
    commit then returns success and discards its documents in silence."""
    text = str(exc)
    if not text.startswith("An IO error occurred:"):
        return False
    # The English phrases are FormatMessage output and are localised, so a
    # German or Japanese Windows would never retry on text alone. The numeric
    # forms are anchored on the closing paren: bare "os error 5" also matches
    # 50-59, six of which are network-share failures.
    return any(
        marker in text
        for marker in (
            "(os error 5)",
            "(os error 32)",
            "Access is denied",
            "being used by another process",
        )
    )


def _commit_attempts(writer: IndexWriter) -> Iterable[float]:
    """Commit; yield the delay to wait before each re-attempt.

    Tantivy replaces ``meta.json`` by renaming a temp file over it; on Windows
    that rename gives ``os error 5`` while another handle holds the destination.
    A failed commit leaves the writer usable with its documents still pending."""
    for delay in _COMMIT_RETRY_DELAYS:
        try:
            writer.commit()
            return
        except ValueError as exc:
            if not _commit_is_retryable(exc):
                raise
            yield delay
    writer.commit()


def commit(writer: IndexWriter) -> None:
    """Commit, waiting out a transient Windows lock on the index metadata."""
    import time

    for delay in _commit_attempts(writer):
        time.sleep(delay)


async def commit_async(writer: IndexWriter) -> None:
    """:func:`commit` for the async runner — yields the loop between attempts."""
    import asyncio

    for delay in _commit_attempts(writer):
        await asyncio.sleep(delay)


def _skip_stamp() -> str:
    """ISO-8601 UTC second-precision timestamp for the [fnd skip ...]
    prefix; matches the form used by the async indexer runner."""
    return _dt.datetime.now(tz=_dt.UTC).isoformat(timespec="seconds")


def _ensure_index(index_dir: Path, *, force: bool = False) -> Index:
    """Open or initialise the Tantivy index at ``index_dir``.

    Two correctness gates:

    1. The ``.fnd-schema-version`` sidecar must match ``SCHEMA_VERSION``;
       on mismatch we either raise (default) or wipe the dir (``force=True``).
    2. Tantivy itself stores the schema in ``meta.json`` and rejects any
       constructor call whose schema doesn't match the on-disk segments.
       Both gates can disagree — e.g. if a prior rebuild bumped the
       sidecar but crashed before Tantivy wrote new segments — so we
       also retry on Tantivy's ``ValueError`` when ``force=True``,
       wiping and re-opening with a fresh schema.
    """
    index_dir = index_dir.expanduser().resolve()
    index_dir.mkdir(parents=True, exist_ok=True)
    schema = build_schema()
    sidecar = index_dir / ".fnd-schema-version"
    if sidecar.exists():
        existing = sidecar.read_text(encoding="utf-8").strip()
        if existing != str(SCHEMA_VERSION):
            if not force:
                raise RuntimeError(
                    f"index at {index_dir} has schema version {existing}; current is "
                    f"{SCHEMA_VERSION}. Rebuild with --rebuild."
                )
            _wipe_index_dir(index_dir, sidecar)
    else:
        sidecar.write_text(str(SCHEMA_VERSION), encoding="utf-8")

    try:
        return Index(schema, path=str(index_dir))
    except ValueError as e:
        # Tantivy's "Schema error: ... does not match" — the sidecar said
        # OK but Tantivy disagrees (e.g. recovery from a half-completed
        # rebuild). With force=True we wipe and retry once; otherwise we
        # surface a clearer recovery instruction.
        if "schema" not in str(e).lower():
            raise
        if not force:
            raise RuntimeError(
                f"index at {index_dir} has an inconsistent schema state. "
                f"Rebuild with `fnd collection reindex <name> --rebuild`."
            ) from e
        _wipe_index_dir(index_dir, sidecar)
        return Index(schema, path=str(index_dir))


def _wipe_index_dir(index_dir: Path, sidecar: Path) -> None:
    """Clear every entry under ``index_dir`` and re-establish the sidecar
    at the current ``SCHEMA_VERSION``. Used by the rebuild path when
    Tantivy can't migrate the on-disk segments in place."""
    import shutil

    for entry in index_dir.iterdir():
        if entry.is_dir():
            shutil.rmtree(entry)
        else:
            entry.unlink()
    sidecar.write_text(str(SCHEMA_VERSION), encoding="utf-8")


def _doc_for_chunk(
    chunk: Chunk,
    *,
    collection: str,
    source_path: str = "",
    meta_blob_bytes: bytes = b"",
    tags: dict[str, frozenset[str]] | None = None,
) -> Document:
    doc = Document()
    doc.add_text(F_PARENT_ID, chunk.parent_id)
    doc.add_text(F_COLLECTION, collection)
    doc.add_text(F_SOURCE_PATH, source_path)
    doc.add_text(F_PATH, chunk.path)
    doc.add_text(F_PATH_TOKENS, chunk.path)
    doc.add_text(F_KIND, chunk.kind)
    doc.add_text(F_HEADING_PATH, chunk.heading_path)
    doc.add_text(F_TITLE, chunk.title)
    doc.add_text(F_AUTHOR, chunk.author)
    doc.add_text(F_BODY, chunk.body)
    doc.add_text(F_PAGE_LABEL, chunk.page_label)
    doc.add_unsigned(F_MTIME, max(chunk.mtime, 0))
    doc.add_unsigned(F_CREATED, max(chunk.created, 0))
    doc.add_unsigned(F_INODE_CTIME, max(chunk.inode_changed, 0))
    doc.add_unsigned(F_PAGE, max(chunk.page, 0))
    doc.add_unsigned(F_SLIDE, max(chunk.slide, 0))
    doc.add_unsigned(F_LINE, max(chunk.line, 0))
    doc.add_unsigned(F_CHUNK_SEQ, max(chunk.chunk_seq, 0))
    doc.add_bytes(F_BODY_STRUCT, encode_body_struct(chunk.body_struct))
    doc.add_bytes(F_BODY_MD, chunk.body_md.encode("utf-8"))
    doc.add_bytes(F_META_BLOB, meta_blob_bytes)
    # One field per provenance. Unknown source ids are skipped so a provider
    # added in a newer build can't break an older writer.
    for source, values in (tags or {}).items():
        field_name = TAG_FIELD_BY_SOURCE.get(source)
        if field_name is None:
            continue
        for value in sorted(values):
            doc.add_text(field_name, value)
    return doc


def read_file_metadata(
    path: Path,
    *,
    tag_sources: Sequence[str] = ("frontmatter", "os"),
    frontmatter_keys: Sequence[str] = (),
) -> tuple[bytes, dict[str, frozenset[str]]]:
    """``(meta_blob_bytes, tags)`` for one file.

    Shared by both index builders so an ad-hoc ``fnd index <root>`` and a
    configured reindex capture identical metadata. Frontmatter is parsed once
    and handed to the tag providers rather than re-read.
    """
    import sys as _sys

    from fnd.frontmatter import FrontmatterParseError, read_frontmatter_from_file
    from fnd.tags import TagContext, providers_for, read_tags

    meta_blob_bytes = b""
    frontmatter: dict[str, object] | None = None
    if path.suffix.lower() == ".md":
        try:
            frontmatter = read_frontmatter_from_file(path)
        except FrontmatterParseError:
            frontmatter = None
        if frontmatter:
            meta_blob_bytes = encode_meta_blob(frontmatter)

    tags = read_tags(
        TagContext(path=path, frontmatter=frontmatter),
        providers_for(_sys.platform, tag_sources, frontmatter_keys=frontmatter_keys),
    )
    return meta_blob_bytes, tags


def build_index(
    *,
    roots: Sequence[Path],
    index_dir: Path,
    collection: str = "default",
    includes: list[str] | None = None,
    excludes: list[str] | None = None,
    follow_symlinks: bool = False,
    rebuild: bool = False,
    tag_sources: Sequence[str] = ("frontmatter", "os"),
    tag_frontmatter_keys: Sequence[str] = (),
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
        commit(writer)

    written = 0
    live_parent_ids: set[str] = set()
    paths: Iterable[Path] = walk(
        roots=roots,
        includes=includes,
        excludes=excludes,
        follow_symlinks=follow_symlinks,
    )
    for path in paths:
        # Idempotent re-index: delete chunks for THIS collection's
        # copy of this file then re-add. Scoped by collection so a
        # file shared across multiple collections (typical: Obsidian
        # Vault listed under several collection sources) keeps the
        # sibling collections' chunks intact.
        parent_id = _path_parent_id(path)
        live_parent_ids.add(parent_id)
        _delete_q = _scoped_delete_query(index.schema, collection, parent_id)
        writer.delete_documents_by_query(_delete_q)
        meta_blob_bytes, file_tags = read_file_metadata(
            path, tag_sources=tag_sources, frontmatter_keys=tag_frontmatter_keys
        )
        try:
            for chunk in extract(path):
                writer.add_document(
                    _doc_for_chunk(
                        chunk,
                        collection=collection,
                        meta_blob_bytes=meta_blob_bytes,
                        tags=file_tags,
                    )
                )
                written += 1
                if written % _COMMIT_BATCH == 0:
                    commit(writer)
        except ExtractError as err:
            writer.delete_documents_by_query(_delete_q)
            print(f"[fnd skip {_skip_stamp()}] {err}", file=sys.stderr)
    commit(writer)
    # See build_index_from_config: skip the prune when a root is missing, or
    # an offline volume would read as "every file was deleted".
    if not rebuild and sources_are_enumerable(Path(r) for r in roots):
        prune_removed_files(index, writer, collection=collection, live_parent_ids=live_parent_ids)
        commit(writer)
    writer.wait_merging_threads()
    return written


def build_index_from_config(
    *,
    config: CollectionConfig,
    collection: str,
    index_dir: Path,
    rebuild: bool = False,
    tag_sources: Sequence[str] = ("frontmatter", "os"),
    tag_frontmatter_keys: Sequence[str] = (),
) -> int:
    """Build a collection from its :class:`CollectionConfig`.

    Walks each source's filter chain via :func:`fnd.walk.walk_sources`
    and indexes the surviving paths. The legacy flat-shape config is
    auto-promoted to a single implicit source by the loader, so this
    function only sees the new shape. For md files, frontmatter is read
    once per file and serialized into ``meta_blob`` on every chunk so the
    query-time post-filter (§5.5e-2) can decode + evaluate it.
    """
    from fnd.walk import walk_sources

    index = _ensure_index(index_dir, force=rebuild)
    writer = index.writer(heap_size=_WRITER_HEAP)
    if rebuild:
        writer.delete_documents(F_COLLECTION, collection)
        commit(writer)
    written = 0
    live_parent_ids: set[str] = set()
    # Walk per-source so each chunk carries an identifier of which
    # source it came from — lets the search layer scope to a subset of
    # a collection's sources without re-indexing.
    for source in config.sources:
        source_id = str(Path(source.path).expanduser().resolve())
        for path in walk_sources(sources=[source]):
            meta_blob_bytes, file_tags = read_file_metadata(
                path, tag_sources=tag_sources, frontmatter_keys=tag_frontmatter_keys
            )
            parent_id = _path_parent_id(path)
            live_parent_ids.add(parent_id)
            _delete_q = _scoped_delete_query(index.schema, collection, parent_id)
            writer.delete_documents_by_query(_delete_q)
            try:
                for chunk in extract(path):
                    writer.add_document(
                        _doc_for_chunk(
                            chunk,
                            collection=collection,
                            source_path=source_id,
                            meta_blob_bytes=meta_blob_bytes,
                            tags=file_tags,
                        )
                    )
                    written += 1
                    if written % _COMMIT_BATCH == 0:
                        commit(writer)
            except ExtractError as err:
                # See build_index above — re-stage the same scoped
                # delete so an extractor crash mid-iteration doesn't
                # leave partial chunks indexed.
                writer.delete_documents_by_query(_delete_q)
                print(f"[fnd skip {_skip_stamp()}] {err}", file=sys.stderr)
    commit(writer)
    # Rebuild already wiped the collection, so nothing can be stale.
    if not rebuild:
        roots = [Path(s.path).expanduser() for s in config.sources]
        if sources_are_enumerable(roots):
            prune_removed_files(
                index, writer, collection=collection, live_parent_ids=live_parent_ids
            )
            commit(writer)
        else:
            missing = ", ".join(str(r) for r in roots if not r.exists())
            print(
                f"[fnd skip {_skip_stamp()}] source unavailable ({missing}); "
                f"kept existing chunks for collection {collection}",
                file=sys.stderr,
            )
    writer.wait_merging_threads()
    return written


def _path_parent_id(path: Path) -> str:
    """Mirror of the extractor's hashing so deletes target the right docs."""
    import hashlib

    return hashlib.sha1(str(path.resolve()).encode("utf-8"), usedforsecurity=False).hexdigest()


# Terms-aggregation bucket cap when enumerating a collection's indexed files.
# Truncation fails safe: fewer buckets means a smaller stale set, so we
# under-prune rather than delete something still live.
_MAX_INDEXED_FILE_BUCKETS = 200_000


def sources_are_enumerable(roots: Iterable[Path]) -> bool:
    """True when every root exists, so an empty walk means "no files" rather
    than "the volume went away".

    :func:`fnd.walk.walk` yields nothing for a missing root instead of
    raising, so an unguarded prune would erase a whole collection the first
    time an external drive or an iCloud folder was offline. Callers must gate
    :func:`prune_removed_files` on this.
    """
    return all(root.exists() for root in roots)


def indexed_parent_ids(index: Index, collection: str) -> set[str]:
    """Every distinct ``parent_id`` currently indexed under ``collection``.

    Uses the fast-field terms aggregation rather than paging documents: one
    bucket per file instead of one hit per chunk.
    """
    import tantivy as _tantivy

    agg: dict[str, object] = {
        "files": {"terms": {"field": F_PARENT_ID, "size": _MAX_INDEXED_FILE_BUCKETS}}
    }
    scope = _tantivy.Query.term_query(index.schema, F_COLLECTION, collection)
    raw = index.searcher().aggregate(scope, agg)
    return {str(b["key"]) for b in raw["files"]["buckets"]}


def prune_removed_files(
    index: Index,
    writer: IndexWriter,
    *,
    collection: str,
    live_parent_ids: set[str],
) -> int:
    """Delete ``collection``'s chunks for files it no longer contains.

    A file leaves a collection by being deleted from disk, excluded by a new
    glob, failing a ``frontmatter_filter``, or having its whole source dropped
    from the config. Re-indexing only ever deleted-and-re-added the files it
    walked, so in all four cases the old chunks lingered and kept turning up
    in results.

    ``live_parent_ids`` must be every file the walk yielded, including ones
    skipped as unchanged and ones that failed to extract — anything missing
    from it is treated as gone. Returns the number of files pruned. The
    caller commits.
    """
    index.reload()
    stale = indexed_parent_ids(index, collection) - live_parent_ids
    for parent_id in stale:
        writer.delete_documents_by_query(_scoped_delete_query(index.schema, collection, parent_id))
    return len(stale)


def _scoped_delete_query(schema: Schema, collection: str, parent_id: str) -> Query:
    """Build a boolean Query that matches a single file's chunks
    within a single collection.

    Required because plain ``delete_documents(F_PARENT_ID, parent_id)``
    is unscoped: a file present in multiple collections (the same
    Obsidian Vault listed under several collection sources, say)
    would lose its chunks from EVERY collection whenever any one
    collection re-indexed it. The boolean form (parent_id AND
    collection) keeps each collection's view isolated."""
    import tantivy as _tantivy

    parent_q = _tantivy.Query.term_query(schema, F_PARENT_ID, parent_id)
    collection_q = _tantivy.Query.term_query(schema, F_COLLECTION, collection)
    return _tantivy.Query.boolean_query(
        [
            (_tantivy.Occur.Must, parent_q),
            (_tantivy.Occur.Must, collection_q),
        ]
    )
