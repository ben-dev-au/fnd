"""Query layer: parse → search → group-by-parent → top-N sections per file.

Phase 1: single-pass query, no rerank. Phase 7 adds the reranker (recency,
filetype, phrase-proximity); phase 8 adds cascading multi-pass; phase 9 adds
RRF fusion of parallel sub-queries.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Final

from tantivy import Index

from acorn.schema import (
    DEFAULT_FIELD_BOOSTS,
    DEFAULT_SEARCH_FIELDS,
    F_BODY_STRUCT,
    F_HEADING_PATH,
    F_KIND,
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
            f"index at {index_dir} schema version mismatch; rebuild with "
            f"`acorn index --rebuild`"
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
    ) -> list[Hit]:
        full_query = query
        if collection:
            full_query = f'collection:"{collection}" AND ({query})'
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
                )
            )
        return out

    def search(
        self,
        query: str,
        *,
        limit: int = _DEFAULT_LIMIT,
        collection: str | None = None,
    ) -> list[Hit]:
        """Return one Hit per file (the file's best-scored chunk).

        Use :meth:`search_grouped` to keep all matched sections of each file.
        """
        if not query.strip():
            return []
        # Pull deeper than ``limit`` so per-file dedup still leaves us with
        # ``limit`` distinct files when several chunks of the same file rank
        # high.
        raw = self._raw_hits(query, limit=limit * 5, collection=collection)
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

    def search_grouped(
        self,
        query: str,
        *,
        limit: int = _DEFAULT_LIMIT,
        sections_per_file: int = 5,
        collection: str | None = None,
    ) -> list[FileGroup]:
        """Return ranked FileGroups, each with up to ``sections_per_file`` ranked
        section hits. Files are sorted by their top-scoring chunk; sections
        within a file are sorted by score (which generally matches document
        order on a single keyword query, but doesn't have to)."""
        if not query.strip():
            return []
        raw = self._raw_hits(query, limit=limit * 10, collection=collection)
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
