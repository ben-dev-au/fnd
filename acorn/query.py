"""Query layer: parse → search → group-by-parent → top-N sections per file.

Phase 1: single-pass query, no rerank. Phase 7 adds the reranker (recency,
filetype, phrase-proximity); phase 8 adds cascading multi-pass; phase 9 adds
RRF fusion of parallel sub-queries. Phase 5.5e-2 adds the optional
``metadata_filter`` kwarg on :meth:`Searcher.search` and
:meth:`Searcher.search_grouped`: a DSL string (same grammar as the
index-time ``frontmatter_filter``) is compiled once and applied as a
post-rank predicate against each md chunk's stored ``meta_blob``, with
oversample-and-retry inside :meth:`Searcher._filtered_raw_hits` so the
caller still gets ``limit`` survivors when the filter is strict.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Final

from tantivy import Index

from acorn.extract.base import Block
from acorn.schema import (
    DEFAULT_FIELD_BOOSTS,
    DEFAULT_SEARCH_FIELDS,
    F_BODY_STRUCT,
    F_CHUNK_SEQ,
    F_HEADING_PATH,
    F_KIND,
    F_META_BLOB,
    F_MTIME,
    F_PAGE,
    F_PARENT_ID,
    F_PATH,
    F_SLIDE,
    F_TITLE,
    SCHEMA_VERSION,
    build_schema,
)

_SNIPPET_CTX = 240
_DEFAULT_LIMIT: Final = 10


@dataclass(slots=True, frozen=True)
class Hit:
    score: float
    parent_id: str
    path: str
    kind: str
    page: int
    slide: int
    heading_path: str
    title: str
    snippet: str
    chunk_seq: int = 0
    # Unix epoch seconds; 0 means "unknown / unindexed file". Used by the
    # reranker (§4 recency boost) — pulled from the F_MTIME fast field at
    # search time, not stored on the Hit until reranking runs.
    mtime: int = 0
    # Cascade pass that produced this hit (§9c): 0 = exact, 1 = fuzzy,
    # 2 = synonym. Used by the TUI to render a per-pass glyph (●/~/⊕).
    pass_index: int = 0
    # JSON-encoded frontmatter for the file (md only); empty bytes for
    # non-md or md without frontmatter. Read at search time from F_META_BLOB
    # so query-time post-filters (§5.5e-2) can decode and evaluate.
    meta_blob: bytes = b""


@dataclass(slots=True, frozen=True)
class FileChunk:
    """One indexed chunk in document order — used by the TUI's full-document
    preview. ``score`` is None for chunks that didn't match the query."""

    parent_id: str
    path: str
    kind: str
    page: int
    slide: int
    heading_path: str
    chunk_seq: int
    blocks: list[Block]
    score: float | None = None


@dataclass(slots=True, frozen=True)
class FileGroup:
    """One file with its ranked matched sections.

    The TUI tree (phase 5) renders the file as a parent node and ``hits`` as
    its sorted children. ``top_score`` mirrors ``hits[0].score`` for sorting.
    """

    parent_id: str
    path: str
    kind: str
    title: str
    top_score: float
    hits: list[Hit]


def _open_index(index_dir: Path) -> Index:
    sidecar = index_dir / ".acorn-schema-version"
    if not sidecar.exists():
        raise FileNotFoundError(f"no acorn index at {index_dir}")
    if sidecar.read_text().strip() != str(SCHEMA_VERSION):
        raise RuntimeError(
            f"index at {index_dir} schema version mismatch; rebuild with `acorn index --rebuild`"
        )
    return Index(build_schema(), path=str(index_dir))


def _first_str(doc: object, field: str) -> str:
    val = doc.get_first(field)  # type: ignore[attr-defined]
    if val is None:
        return ""
    return str(val)


def _first_int(doc: object, field: str) -> int:
    val = doc.get_first(field)  # type: ignore[attr-defined]
    if val is None:
        return 0
    return int(val)


def _make_snippet(body_text: str, query: str, *, ctx: int = _SNIPPET_CTX) -> str:
    """Return a short snippet centered on the first query-term match."""
    if not body_text:
        return ""
    lower = body_text.lower()
    needle = ""
    for term in query.lower().split():
        # Pick the first term that actually appears.
        if term in lower:
            needle = term
            break
    if not needle:
        return body_text[:ctx].strip()
    pos = lower.find(needle)
    start = max(0, pos - ctx // 2)
    end = min(len(body_text), pos + ctx // 2)
    snippet = body_text[start:end].replace("\n", " ").strip()
    return snippet


def _passes_meta_filter(hit: Hit, predicate: object) -> bool:
    """Apply ``predicate`` to a hit's frontmatter. Non-md hits bypass the
    filter entirely (md-only semantics matching :func:`acorn.walk.walk_sources`).
    """
    if hit.kind != "md":
        return True
    from acorn.meta_blob import decode

    fm = decode(hit.meta_blob)
    return bool(predicate(fm))  # type: ignore[operator]


class Searcher:
    """Single-pass searcher against an existing acorn index."""

    def __init__(self, *, index_dir: Path) -> None:
        self._index = _open_index(index_dir)
        self._index.reload()
        self._searcher = self._index.searcher()

    def _raw_hits(
        self,
        query: str,
        *,
        limit: int,
        collection: str | None,
        active_sources: list[str] | None = None,
    ) -> list[Hit]:
        from acorn.query_dsl import preprocess

        full_query = preprocess(query)
        if collection:
            full_query = f'collection:"{collection}" AND ({full_query})'
        if active_sources:
            src_clause = " OR ".join(f'source_path:"{s}"' for s in active_sources)
            full_query = f"({src_clause}) AND ({full_query})"
        parsed = self._index.parse_query(
            full_query,
            default_field_names=DEFAULT_SEARCH_FIELDS,
            field_boosts=DEFAULT_FIELD_BOOSTS,
        )
        result = self._searcher.search(parsed, limit=limit)

        from acorn.struct import decode as decode_body_struct

        out: list[Hit] = []
        for score, address in result.hits:
            doc = self._searcher.doc(address)
            body_struct_bytes = doc.get_first(F_BODY_STRUCT)  # type: ignore[attr-defined]
            body_text = ""
            if body_struct_bytes is not None:
                blocks = decode_body_struct(body_struct_bytes)
                body_text = "\n".join(b.text for b in blocks)
            meta_blob_bytes = doc.get_first(F_META_BLOB)  # type: ignore[attr-defined]
            if meta_blob_bytes is None:
                meta_blob_bytes = b""
            out.append(
                Hit(
                    score=float(score),
                    parent_id=_first_str(doc, F_PARENT_ID),
                    path=_first_str(doc, F_PATH),
                    kind=_first_str(doc, F_KIND),
                    page=_first_int(doc, F_PAGE),
                    slide=_first_int(doc, F_SLIDE),
                    heading_path=_first_str(doc, F_HEADING_PATH),
                    title=_first_str(doc, F_TITLE),
                    snippet=_make_snippet(body_text, query),
                    chunk_seq=_first_int(doc, F_CHUNK_SEQ),
                    mtime=_first_int(doc, F_MTIME),
                    meta_blob=meta_blob_bytes,
                )
            )
        return out

    def _filtered_raw_hits(
        self,
        query: str,
        *,
        target: int,
        collection: str | None,
        metadata_filter: str | None,
        active_sources: list[str] | None = None,
    ) -> list[Hit]:
        """Return at least ``target`` hits, applying the optional metadata
        filter post-Tantivy with oversample-and-retry."""
        if metadata_filter is None:
            return self._raw_hits(
                query, limit=target, collection=collection, active_sources=active_sources
            )
        from acorn.filter_dsl import compile_filter

        predicate = compile_filter(metadata_filter)
        oversample = 1
        max_oversample = 50
        while True:
            raw = self._raw_hits(
                query,
                limit=target * oversample,
                collection=collection,
                active_sources=active_sources,
            )
            survivors = [h for h in raw if _passes_meta_filter(h, predicate)]
            if len(survivors) >= target:
                return survivors
            if oversample >= max_oversample:
                return survivors
            if len(raw) < target * oversample:
                return survivors
            oversample *= 2

    def search(
        self,
        query: str,
        *,
        limit: int = _DEFAULT_LIMIT,
        collection: str | None = None,
        profile: object | None = None,
        now: int | None = None,
        metadata_filter: str | None = None,
        active_sources: list[str] | None = None,
    ) -> list[Hit]:
        """Return one Hit per file (the file's best-scored chunk).

        Use :meth:`search_grouped` to keep all matched sections of each file.
        When ``profile`` is set, applies the §4 Python post-rank adjustments
        (recency / filetype / phrase-proximity) before per-file dedup.
        ``active_sources`` further narrows scope to chunks indexed from
        the listed source paths.
        """
        if not query.strip():
            return []
        raw = self._filtered_raw_hits(
            query,
            target=limit * 5,
            collection=collection,
            metadata_filter=metadata_filter,
            active_sources=active_sources,
        )
        if profile is not None:
            from acorn.rerank import RankingProfile, rerank_hits

            assert isinstance(profile, RankingProfile)
            raw = rerank_hits(raw, profile=profile, query=query, now=now)
        seen: set[str] = set()
        out: list[Hit] = []
        for h in raw:
            if h.parent_id in seen:
                continue
            seen.add(h.parent_id)
            out.append(h)
            if len(out) >= limit:
                break
        return out

    def get_file_chunks(self, parent_id: str) -> list[FileChunk]:
        """Return every indexed chunk of the file identified by ``parent_id``,
        ordered by ``chunk_seq``. Used by the TUI for the full-document preview.

        Implementation: query for ``parent_id:<id>`` with a wide limit and
        decode each chunk's stored body_struct.
        """
        from acorn.struct import decode as decode_body_struct

        parsed = self._index.parse_query(
            f'parent_id:"{parent_id}"',
            default_field_names=[F_PARENT_ID],
        )
        # 5000 chunks/file is a generous ceiling; phase 12 will revisit for
        # books / very long PDFs.
        result = self._searcher.search(parsed, limit=5000)
        chunks: list[FileChunk] = []
        for _score, address in result.hits:
            doc = self._searcher.doc(address)
            body_struct_bytes = doc.get_first(F_BODY_STRUCT)  # type: ignore[attr-defined]
            blocks = decode_body_struct(body_struct_bytes) if body_struct_bytes else []
            chunks.append(
                FileChunk(
                    parent_id=_first_str(doc, F_PARENT_ID),
                    path=_first_str(doc, F_PATH),
                    kind=_first_str(doc, F_KIND),
                    page=_first_int(doc, F_PAGE),
                    slide=_first_int(doc, F_SLIDE),
                    heading_path=_first_str(doc, F_HEADING_PATH),
                    chunk_seq=_first_int(doc, F_CHUNK_SEQ),
                    blocks=blocks,
                )
            )
        chunks.sort(key=lambda c: c.chunk_seq)
        return chunks

    def search_grouped(
        self,
        query: str,
        *,
        limit: int = _DEFAULT_LIMIT,
        sections_per_file: int = 5,
        collection: str | None = None,
        profile: object | None = None,
        now: int | None = None,
        metadata_filter: str | None = None,
        active_sources: list[str] | None = None,
    ) -> list[FileGroup]:
        """Return ranked FileGroups, each with up to ``sections_per_file`` ranked
        section hits. ``active_sources`` narrows scope to chunks indexed
        from a subset of the active collection's sources.
        """
        if not query.strip():
            return []
        raw = self._filtered_raw_hits(
            query,
            target=limit * 10,
            collection=collection,
            metadata_filter=metadata_filter,
            active_sources=active_sources,
        )
        if profile is not None:
            from acorn.rerank import RankingProfile, rerank_hits

            assert isinstance(profile, RankingProfile)
            raw = rerank_hits(raw, profile=profile, query=query, now=now)
        groups: dict[str, list[Hit]] = {}
        order: list[str] = []  # parent_ids in first-seen-best-score order
        for h in raw:
            bucket = groups.get(h.parent_id)
            if bucket is None:
                groups[h.parent_id] = [h]
                order.append(h.parent_id)
            else:
                bucket.append(h)
        out: list[FileGroup] = []
        for pid in order[:limit]:
            section_hits = groups[pid][:sections_per_file]
            top = section_hits[0]
            out.append(
                FileGroup(
                    parent_id=pid,
                    path=top.path,
                    kind=top.kind,
                    title=top.title,
                    top_score=top.score,
                    hits=section_hits,
                )
            )
        return out
