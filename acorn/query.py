"""Query layer: parse → search → group-by-parent → top-N sections per file.

Phase 1: single-pass query, no rerank. Phase 7 adds the reranker (recency,
filetype, phrase-proximity); phase 8 adds cascading multi-pass; phase 9 adds
RRF fusion of parallel sub-queries.
"""

from __future__ import annotations

from collections.abc import Iterator
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

    def search(
        self,
        query: str,
        *,
        limit: int = _DEFAULT_LIMIT,
        collection: str | None = None,
    ) -> list[Hit]:
        if not query.strip():
            return []
        # Tantivy's parser handles boolean / phrase / fuzzy / field syntax natively.
        # Per §22 Spike A: per-field boosts are query-time tunable.
        full_query = query
        if collection:
            full_query = f'collection:"{collection}" AND ({query})'
        parsed = self._index.parse_query(
            full_query,
            default_field_names=DEFAULT_SEARCH_FIELDS,
            field_boosts=DEFAULT_FIELD_BOOSTS,
        )
        result = self._searcher.search(parsed, limit=limit)

        # Group by parent_id so each file's best chunk wins; later phases will
        # nest sub-hits under each file in the TUI tree.
        seen_parents: dict[str, Hit] = {}
        ordered: list[Hit] = []
        for score, address in result.hits:
            doc = self._searcher.doc(address)
            parent_id = _first_str(doc, F_PARENT_ID)
            if parent_id in seen_parents:
                continue
            body_struct_bytes = doc.get_first(F_BODY_STRUCT)  # type: ignore[attr-defined]
            body_text = ""
            if body_struct_bytes is not None:
                from acorn.struct import decode as decode_body_struct

                blocks = decode_body_struct(body_struct_bytes)
                body_text = "\n".join(b.text for b in blocks)
            hit = Hit(
                score=float(score),
                parent_id=parent_id,
                path=_first_str(doc, F_PATH),
                kind=_first_str(doc, F_KIND),
                page=_first_int(doc, F_PAGE),
                slide=_first_int(doc, F_SLIDE),
                heading_path=_first_str(doc, F_HEADING_PATH),
                title=_first_str(doc, F_TITLE),
                snippet=_make_snippet(body_text, query),
            )
            seen_parents[parent_id] = hit
            ordered.append(hit)
        return ordered


# ── CLI helper ─────────────────────────────────────────────────────────────


def search_text(query: str, *, limit: int = _DEFAULT_LIMIT) -> Iterator[str]:
    """Yield ranked ``file:locator snippet`` lines for the CLI ``search`` cmd."""
    from acorn.config import default_index_dir

    searcher = Searcher(index_dir=default_index_dir())
    for hit in searcher.search(query, limit=limit):
        loc = ""
        if hit.page:
            loc = f":p.{hit.page}"
        elif hit.slide:
            loc = f":s.{hit.slide}"
        elif hit.heading_path:
            loc = f" §{hit.heading_path}"
        yield f"{hit.score:6.3f}  {hit.path}{loc}\n        {hit.snippet}"
