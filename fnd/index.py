"""Tantivy IndexWriter wrapper + ``build_index`` entry point.

Single-process, single-writer.
"""

from __future__ import annotations

import datetime as _dt
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path

from tantivy import Document, Index, IndexWriter, Query, Schema

from fnd.config import CollectionConfig
from fnd.extract import Chunk, ExtractError, extract, no_text_reason
from fnd.membership import after_index, after_prune
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
    F_MEMBERSHIP,
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
    MEMBERSHIP_SEP,
    SCHEMA_VERSION,
    TAG_FIELD_BY_SOURCE,
    build_schema,
    membership_token,
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
    memberships: Iterable[tuple[str, str]],
    meta_blob_bytes: bytes = b"",
    tags: dict[str, frozenset[str]] | None = None,
) -> Document:
    doc = Document()
    doc.add_text(F_PARENT_ID, chunk.parent_id)
    # A file is stored once; F_COLLECTION and F_SOURCE_PATH are the distinct
    # collections/sources it belongs to, and F_MEMBERSHIP the exact pairs.
    pairs = sorted(set(memberships))
    for collection in sorted({c for c, _ in pairs}):
        doc.add_text(F_COLLECTION, collection)
    for source in sorted({s for _, s in pairs}):
        doc.add_text(F_SOURCE_PATH, source)
    for collection, source in pairs:
        doc.add_text(F_MEMBERSHIP, membership_token(collection, source))
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


def _extract_docs(
    path: Path,
    *,
    memberships: frozenset[tuple[str, str]],
    meta_blob_bytes: bytes,
    tags: dict[str, frozenset[str]] | None,
) -> list[Document]:
    """Every chunk document for a file, built before any index mutation.

    Extraction is the failure-prone step, so it runs in full before the caller
    deletes the prior document: an ``ExtractError`` then leaves the existing
    copy, and any sibling collection's membership on it, untouched.
    """
    return [
        _doc_for_chunk(chunk, memberships=memberships, meta_blob_bytes=meta_blob_bytes, tags=tags)
        for chunk in extract(path)
    ]


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

    from fnd.file_facts import frontmatter_kinds
    from fnd.frontmatter import FrontmatterParseError, read_frontmatter_from_file
    from fnd.kinds import kind_for_suffix
    from fnd.tags import TagContext, providers_for, read_tags

    meta_blob_bytes = b""
    frontmatter: dict[str, object] | None = None
    # The registry's word, not the suffix: `carries_frontmatter` is the single
    # answer precisely so this cannot drift, and asking for `.md` here dropped
    # the tags off every other Markdown variant as well as off `.txt`.
    if kind_for_suffix(path.suffix) in frontmatter_kinds():
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

    Honours the includes/excludes glob precedence rules. Returns the number of
    chunks written. Single-process, single-writer.

    Every walked file is re-extracted and re-added with its membership merged,
    so a file shared across collections keeps the others' membership. Files the
    walk no longer reaches are pruned. ``rebuild`` forces a fresh schema when
    the on-disk one is stale (see :func:`_ensure_index`).
    """
    index = _ensure_index(index_dir, force=rebuild)
    writer = index.writer(heap_size=_WRITER_HEAP)
    searcher = index.searcher()

    written = 0
    live_parent_ids: set[str] = set()
    paths: Iterable[Path] = walk(
        roots=roots,
        includes=includes,
        excludes=excludes,
        follow_symlinks=follow_symlinks,
    )
    for path in paths:
        parent_id = _path_parent_id(path)
        live_parent_ids.add(parent_id)
        memberships = after_index(
            read_membership(searcher, index.schema, parent_id), collection, ""
        )
        meta_blob_bytes, file_tags = read_file_metadata(
            path, tag_sources=tag_sources, frontmatter_keys=tag_frontmatter_keys
        )
        try:
            docs = _extract_docs(
                path, memberships=memberships, meta_blob_bytes=meta_blob_bytes, tags=file_tags
            )
        except ExtractError as err:
            # Extraction failed: leave the prior document, and any sibling
            # collection's membership on it, untouched.
            print(f"[fnd skip {_skip_stamp()}] {err}", file=sys.stderr)
            continue
        if not docs:
            # No text and no error is treated like a failure: keep what is
            # there rather than delete on a possibly transient empty read.
            print(f"[fnd skip {_skip_stamp()}] {no_text_reason(path)}", file=sys.stderr)
            continue
        writer.delete_documents_by_query(_parent_delete_query(index.schema, parent_id))
        for doc in docs:
            writer.add_document(doc)
            written += 1
            if written % _COMMIT_BATCH == 0:
                commit(writer)
    commit(writer)
    # Skip the prune when a root is missing, or an offline volume would read
    # as "every file was deleted".
    if sources_are_enumerable(Path(r) for r in roots):
        prune_removed_files(
            index,
            writer,
            collection=collection,
            live_parent_ids=live_parent_ids,
            tag_sources=tag_sources,
            tag_frontmatter_keys=tag_frontmatter_keys,
        )
        commit(writer)
    writer.wait_merging_threads()
    return written


def build_index_from_config(
    *,
    config: CollectionConfig,
    collection: str,
    index_dir: Path,
    rebuild: bool = False,
    prune: bool = True,
    tag_sources: Sequence[str] = ("frontmatter", "os"),
    tag_frontmatter_keys: Sequence[str] = (),
) -> int:
    """Build a collection from its :class:`CollectionConfig`.

    Walks each source's filter chain via :func:`fnd.walk.walk_sources`
    and indexes the surviving paths. The legacy flat-shape config is
    auto-promoted to a single implicit source by the loader, so this
    function only sees the new shape. For md files, frontmatter is read
    once per file and serialized into ``meta_blob`` on every chunk so the
    query-time post-filter can decode + evaluate it.

    ``prune`` drops the collection's documents this walk did not reach, which
    is how a file deleted from disk leaves the index. A caller indexing PART
    of a collection must pass False: everything else in it is not stale, it is
    simply not in this walk.
    """
    from fnd.walk import walk_sources

    index = _ensure_index(index_dir, force=rebuild)
    writer = index.writer(heap_size=_WRITER_HEAP)
    searcher = index.searcher()
    written = 0
    live_parent_ids: set[str] = set()
    # Walk per-source so each file records which source reached it, letting the
    # search layer scope to a subset of a collection's sources. A file reachable
    # from two of this collection's sources is owned by the first (claimed).
    claimed: set[str] = set()
    for source in config.sources:
        source_id = str(Path(source.path).expanduser().resolve())
        for path in walk_sources(sources=[source]):
            key = str(path.resolve())
            if key in claimed:
                continue
            claimed.add(key)
            meta_blob_bytes, file_tags = read_file_metadata(
                path, tag_sources=tag_sources, frontmatter_keys=tag_frontmatter_keys
            )
            parent_id = _path_parent_id(path)
            live_parent_ids.add(parent_id)
            memberships = after_index(
                read_membership(searcher, index.schema, parent_id), collection, source_id
            )
            try:
                docs = _extract_docs(
                    path, memberships=memberships, meta_blob_bytes=meta_blob_bytes, tags=file_tags
                )
            except ExtractError as err:
                # Leave the prior document and sibling memberships untouched.
                print(f"[fnd skip {_skip_stamp()}] {err}", file=sys.stderr)
                continue
            if not docs:
                print(f"[fnd skip {_skip_stamp()}] {no_text_reason(path)}", file=sys.stderr)
                continue
            writer.delete_documents_by_query(_parent_delete_query(index.schema, parent_id))
            for doc in docs:
                writer.add_document(doc)
                written += 1
                if written % _COMMIT_BATCH == 0:
                    commit(writer)
    commit(writer)
    if prune:
        roots = [Path(s.path).expanduser() for s in config.sources]
        if sources_are_enumerable(roots):
            prune_removed_files(
                index,
                writer,
                collection=collection,
                live_parent_ids=live_parent_ids,
                tag_sources=tag_sources,
                tag_frontmatter_keys=tag_frontmatter_keys,
            )
            commit(writer)
        else:
            blocked = ", ".join(str(r) for r in unreadable_roots(roots))
            print(
                f"[fnd skip {_skip_stamp()}] source unreadable ({blocked}); "
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
    """True when every root can be LISTED, so an empty walk means "no files"
    rather than "could not look".

    :func:`fnd.walk.walk` yields nothing for a root it cannot read instead of
    raising, so an unguarded prune erases the collection. Callers must gate
    :func:`prune_removed_files` on this.

    Listing, not ``exists()``: a directory with mode 000 exists, and reading
    the walk's silence as "every file was deleted" took a collection from 48
    documents to 0 in one keypress, reported as `Done.`
    """
    return not unreadable_roots(roots)


def unreadable_roots(roots: Iterable[Path]) -> list[Path]:
    """The roots that cannot be listed, in order, with the reason implicit.

    Shared with the gate so a skip message can never name a different root
    from the one that stopped the prune.
    """
    import os

    out: list[Path] = []
    for root in roots:
        try:
            # `walk` yields a file root directly (see fnd/walk.py), so there is
            # nothing to list and stat'ing it is the whole question.
            if root.is_file():
                continue
            with os.scandir(root) as entries:
                next(iter(entries), None)
        except OSError:
            out.append(root)
    return out


def collection_is_empty(index: Index, collection: str) -> bool:
    """Whether ``collection`` holds no documents at all.

    One hit is enough to answer it, so this does not page or aggregate:
    `indexed_parent_ids` builds a set of every file, which is far too much work
    for a row summary that only needs to know "any?".
    """
    import tantivy as _tantivy

    index.reload()
    scope = _tantivy.Query.term_query(index.schema, F_COLLECTION, collection)
    return not index.searcher().search(scope, limit=1).hits


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


def drop_collection(
    index_dir: Path,
    collection: str,
    *,
    tag_sources: Sequence[str] = ("frontmatter", "os"),
    tag_frontmatter_keys: Sequence[str] = (),
) -> None:
    """Remove one collection from the index.

    Paired with a config write that removes the collection's name: whichever
    half is missing, the index keeps documents nothing can reach afterwards.
    A file shared with another collection keeps its document (with this
    collection removed); one held by nothing is deleted.
    """
    index = _ensure_index(index_dir)
    writer = index.writer(heap_size=_WRITER_HEAP)
    for parent_id in indexed_parent_ids(index, collection):
        # No enumerability guard here (the source may already be out of config),
        # so never delete a shared file on a missing path: it could be a
        # transient unmount, and the dropped collection lingering in a sibling's
        # membership is harmless.
        _reduce_membership(
            index,
            writer,
            parent_id=parent_id,
            collection=collection,
            tag_sources=tag_sources,
            tag_frontmatter_keys=tag_frontmatter_keys,
            delete_if_unreachable=False,
        )
    commit(writer)
    writer.wait_merging_threads()


def prune_removed_files(
    index: Index,
    writer: IndexWriter,
    *,
    collection: str,
    live_parent_ids: set[str],
    tag_sources: Sequence[str] = ("frontmatter", "os"),
    tag_frontmatter_keys: Sequence[str] = (),
) -> set[str]:
    """Remove ``collection`` from the membership of files it no longer contains.

    A file leaves a collection by being deleted from disk, excluded by a new
    glob, failing a ``frontmatter_filter``, or having its whole source dropped
    from the config. A file shared with another collection keeps its document
    (with the collection removed); one held by nothing, or gone from disk, is
    deleted. See :func:`_reduce_membership`.

    ``live_parent_ids`` must be every file the walk yielded, including ones
    skipped as unchanged and ones that failed to extract — anything missing
    from it is treated as gone. Returns the pruned ``parent_id``s so the
    caller can say which of them the rest of the index still holds. The
    caller commits.
    """
    index.reload()
    stale = indexed_parent_ids(index, collection) - live_parent_ids
    for parent_id in stale:
        # Callers run prune only when the sources were enumerable, so a file
        # missing from disk is genuinely deleted, not transiently unmounted.
        _reduce_membership(
            index,
            writer,
            parent_id=parent_id,
            collection=collection,
            tag_sources=tag_sources,
            tag_frontmatter_keys=tag_frontmatter_keys,
            delete_if_unreachable=True,
        )
    return stale


def collections_still_holding(
    index: Index, parent_ids: set[str], *, excluding: str
) -> tuple[str, ...]:
    """Other collections that still index any of ``parent_ids``, sorted.

    A file leaving one collection has not left the corpus: a folder listed
    under two collections keeps it, so a run's `N removed` is true of the
    collection and silently false of the index. Costly only in the size of
    ``parent_ids``, which is the number of files that just left.
    """
    if not parent_ids:
        return ()
    import tantivy as _tantivy

    schema = index.schema
    terms = [
        (_tantivy.Occur.Should, _tantivy.Query.term_query(schema, F_PARENT_ID, p))
        for p in parent_ids
    ]
    agg: dict[str, object] = {
        "cols": {"terms": {"field": F_COLLECTION, "size": _MAX_INDEXED_FILE_BUCKETS}}
    }
    raw = index.searcher().aggregate(_tantivy.Query.boolean_query(terms), agg)
    return tuple(sorted({str(b["key"]) for b in raw["cols"]["buckets"]} - {excluding}))


def read_membership(searcher: object, schema: Schema, parent_id: str) -> frozenset[tuple[str, str]]:
    """The (collection, source) pairs currently stored for a file.

    All of a file's chunks share the membership set, so one chunk answers it.
    Returns an empty set when the file is not in the index.
    """
    query = Query.term_query(schema, F_PARENT_ID, parent_id)
    try:
        hits = searcher.search(query, limit=1).hits  # type: ignore[attr-defined]
    except ValueError:
        return frozenset()
    if not hits:
        return frozenset()
    doc = searcher.doc(hits[0][1])  # type: ignore[attr-defined]
    pairs: set[tuple[str, str]] = set()
    for token in doc.get_all(F_MEMBERSHIP):
        collection, _, source = str(token).partition(MEMBERSHIP_SEP)
        pairs.add((collection, source))
    return frozenset(pairs)


def _parent_delete_query(schema: Schema, parent_id: str) -> Query:
    """Match every chunk of one file, across all collections. Normalised
    storage keeps one document per file, so a re-index or prune deletes the
    whole file and re-adds it with the recomputed membership."""
    return Query.term_query(schema, F_PARENT_ID, parent_id)


def _stored_path(searcher: object, schema: Schema, parent_id: str) -> Path | None:
    """The filesystem path stored for a file, or None if it is not indexed."""
    query = Query.term_query(schema, F_PARENT_ID, parent_id)
    try:
        hits = searcher.search(query, limit=1).hits  # type: ignore[attr-defined]
    except ValueError:
        return None
    if not hits:
        return None
    stored = searcher.doc(hits[0][1]).get_first(F_PATH)  # type: ignore[attr-defined]
    return Path(str(stored)) if stored else None


def _reduce_membership(
    index: Index,
    writer: IndexWriter,
    *,
    parent_id: str,
    collection: str,
    tag_sources: Sequence[str],
    tag_frontmatter_keys: Sequence[str],
    delete_if_unreachable: bool,
) -> None:
    """Remove one collection from a file's membership.

    When that empties the membership the file is deleted (no sibling holds it).
    Otherwise it is re-extracted from disk and re-added with the reduced
    membership so sibling collections keep it (re-extraction, not a stored-doc
    rewrite, because ``F_BODY`` is not stored). If the file cannot be read now
    (present but unreadable, e.g. a cloud placeholder, or empty), the existing
    document is LEFT untouched rather than deleted, which would silently remove
    the file from a live sibling collection. The un-reduced entry is harmless
    (a dropped collection builds no scope arm). It does NOT clear on an ordinary
    reindex of the sibling that holds the file: prune only reduces files the
    reindexed collection no longer reaches, and the sibling still reaches this
    one. It clears when the file leaves the index entirely (deleted from disk,
    so the sibling's next prune whole-deletes the document) or a collection of
    the same name is recreated and reindexes without reaching it. A file gone
    from disk is deleted only when ``delete_if_unreachable`` (the caller
    verified the source was enumerable, so absence is genuine, not a transient
    unmount).
    """
    schema = index.schema
    searcher = index.searcher()
    remaining = after_prune(read_membership(searcher, schema, parent_id), collection)
    delete_q = _parent_delete_query(schema, parent_id)
    if not remaining:
        writer.delete_documents_by_query(delete_q)
        return
    path = _stored_path(searcher, schema, parent_id)
    if path is None or not path.exists():
        if delete_if_unreachable:
            writer.delete_documents_by_query(delete_q)
        return
    meta_blob_bytes, file_tags = read_file_metadata(
        path, tag_sources=tag_sources, frontmatter_keys=tag_frontmatter_keys
    )
    try:
        docs = _extract_docs(
            path, memberships=remaining, meta_blob_bytes=meta_blob_bytes, tags=file_tags
        )
    except ExtractError:
        return
    if not docs:
        return
    writer.delete_documents_by_query(delete_q)
    for doc in docs:
        writer.add_document(doc)
