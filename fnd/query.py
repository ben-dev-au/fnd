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

import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

from tantivy import Index, Query, Schema

from fnd.display_text import sanitise_display_text
from fnd.extract.base import Block
from fnd.query_errors import QuerySyntaxError
from fnd.query_errors import QueryTooLargeError as QueryTooLargeError  # re-export (back-compat)
from fnd.query_plan import enforce_query_bounds

if TYPE_CHECKING:
    from fnd.tag_query import TagFilter

from fnd.schema import (
    DEFAULT_FIELD_BOOSTS,
    F_BODY_MD,
    F_BODY_STRUCT,
    F_CHUNK_SEQ,
    F_HEADING_PATH,
    F_KIND,
    F_LINE,
    F_META_BLOB,
    F_MTIME,
    F_PAGE,
    F_PAGE_LABEL,
    F_PARENT_ID,
    F_PATH,
    F_SLIDE,
    F_TITLE,
    SCHEMA_VERSION,
    build_schema,
)

_SNIPPET_CTX = 240
_DEFAULT_LIMIT: Final = 10
# Content tokens that parse_query can't handle on the body field and which we
# resolve against the stemmed dictionary ourselves:
#   _WILDCARD_RE  trailing prefix wildcard ``crypto*``  → BM25 prefix_variants
#   _FUZZY_RE     ``term~N`` fuzzy                       → BM25 fuzzy_variants
#   _REGEX_RE     ``/pattern/``                          → RegexQuery
#   _GLOB_RE      any ``*``/``?`` (infix/leading)        → RegexQuery
_WILDCARD_RE: Final = re.compile(r"^(\w+)\*$")
_FUZZY_RE: Final = re.compile(r"^(\w+)~(\d*)$")
_REGEX_RE: Final = re.compile(r"^/(.+)/$")
_GLOB_RE: Final = re.compile(r"[*?]")
# Below this many chunks/file the thread-pool overhead outweighs the
# decode parallelism — fall back to serial decode regardless of the
# requested ``max_workers``.
_PARALLEL_DECODE_THRESHOLD: Final = 50


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
    # Printed page label (e.g. "292" or "iv"); empty when the PDF has
    # no labels or the chunk isn't a PDF page. Display layers prefer
    # this over ``page`` (which is the PDF page index used by Skim).
    page_label: str = ""
    chunk_seq: int = 0
    # 1-based source line of the chunk's first character (MD heading
    # line, TXT chunk window start). 0 for kinds without line tracking
    # (PDF / DOCX / PPTX). Consumed by the opener for ``{line}``
    # template variables (vscode, sublime, etc.).
    line: int = 0
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
    # Decoded chunk body text (from F_BODY_STRUCT). Carried so the §4
    # phrase-proximity reranker can measure term spread across the whole
    # chunk, not just the ~240-char snippet. Empty until populated.
    body_text: str = ""


@dataclass(slots=True, frozen=True)
class FileChunk:
    """One indexed chunk in document order — used by the TUI's full-document
    preview. ``score`` is None for chunks that didn't match the query.

    ``blocks`` is the legacy plain-text Block list (used by the snippet
    pipeline and as a fallback for the preview pane on stale indexes).
    ``body_md`` is the verbatim or serialised markdown source used by
    the structural preview renderer (Textual Markdown widget) for
    md / docx / pptx chunks; empty for pdf / txt where there is no
    structure to render.
    """

    parent_id: str
    path: str
    kind: str
    page: int
    slide: int
    heading_path: str
    chunk_seq: int
    blocks: list[Block]
    page_label: str = ""
    body_md: str = ""
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
    sidecar = index_dir / ".fnd-schema-version"
    if not sidecar.exists():
        raise FileNotFoundError(f"no fnd index at {index_dir}")
    if sidecar.read_text(encoding="utf-8").strip() != str(SCHEMA_VERSION):
        raise RuntimeError(
            f"index at {index_dir} schema version mismatch; rebuild with `fnd index --rebuild`"
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


def _make_snippet(
    body_text: str,
    query: str,
    *,
    ctx: int = _SNIPPET_CTX,
    intent: str | None = None,
) -> str:
    """Return a short snippet centered on the first query-term match.

    When ``intent`` is supplied (UX-pass-4 §3), prefers a window whose
    context overlaps with intent tokens — picks the first occurrence of
    the query term whose ``±ctx/2`` window contains an intent token.
    Falls back to the first occurrence when no intent-aware candidate
    is found.
    """
    if not body_text:
        return ""
    from fnd.render import _terms_from_query  # lazy: avoid import cycle

    lower = body_text.lower()
    # Clean content terms — strips ``{N}``/``NEAR/N``/``"…"~N``/wildcards/operators/
    # field qualifiers so a proximity or phrase query anchors on real words, not
    # its DSL sigils. A raw ``query.split()`` would skip ``{5}exit`` and centre
    # the window on the wrong lone term (the bug behind "result doesn't match").
    terms = list(dict.fromkeys(t for t in _terms_from_query(query) if t in lower))
    if not terms:
        return sanitise_display_text(body_text[:ctx]).strip()

    half = ctx // 2
    intent_tokens = [t for t in (intent or "").lower().split() if len(t) >= 3]
    # Anchor on the occurrence whose ±half window covers the MOST distinct query
    # terms (the actual proximity / phrase match), then an intent-token overlap,
    # then the earliest position.
    best_pos = lower.find(terms[0])
    best_key: tuple[int, bool, int] | None = None
    for term in terms:
        start = 0
        while (i := lower.find(term, start)) >= 0:
            window = lower[max(0, i - half) : i + half]
            covered = sum(1 for t in terms if t in window)
            has_intent = any(tok in window for tok in intent_tokens)
            key = (covered, has_intent, -i)
            if best_key is None or key > best_key:
                best_key, best_pos = key, i
            start = i + len(term)

    start_idx = max(0, best_pos - half)
    end_idx = min(len(body_text), best_pos + half)
    return sanitise_display_text(body_text[start_idx:end_idx]).strip()


def _passes_meta_filter(hit: Hit, predicate: object) -> bool:
    """Apply ``predicate`` to a hit's frontmatter. Non-md hits bypass the
    filter entirely (md-only semantics matching :func:`fnd.walk.walk_sources`).
    """
    if hit.kind != "md":
        return True
    from fnd.meta_blob import decode

    fm = decode(hit.meta_blob)
    return bool(predicate(fm))  # type: ignore[operator]


def _dedup_by_file(hits: list[Hit], limit: int) -> list[Hit]:
    """First ``limit`` hits with one per ``parent_id`` (the file's best chunk,
    since ``hits`` is already ranked)."""
    seen: set[str] = set()
    out: list[Hit] = []
    for h in hits:
        if h.parent_id in seen:
            continue
        seen.add(h.parent_id)
        out.append(h)
        if len(out) >= limit:
            break
    return out


def _parse_query(index: Index, query: str, **kwargs: object) -> Query:
    """Parse via Tantivy, converting its raw ``ValueError`` syntax errors into a
    typed :class:`QuerySyntaxError` so callers never crash on a malformed query."""
    try:
        return index.parse_query(query, **kwargs)  # type: ignore[arg-type]
    except ValueError as e:
        raise QuerySyntaxError(
            "invalid query syntax",
            hint="check quotes, brackets and parentheses are balanced",
        ) from e


class Searcher:
    """Single-pass searcher against an existing fnd index."""

    def __init__(self, *, index_dir: Path) -> None:
        self._index = _open_index(index_dir)
        self._index.reload()
        self._searcher = self._index.searcher()

    def reload(self) -> None:
        """Re-point at the latest committed index generation.

        The captured ``self._searcher`` reads from the generation it was
        opened against; after a reindex commits new chunks the old
        snapshot still returns the previous generation. ``reload()`` is
        near-free (~0.1 ms) when nothing changed, so it is safe to call
        on the query hot path to keep results current without a restart.
        """
        self._index.reload()
        self._searcher = self._index.searcher()

    def _content_body_query(
        self, content: str, schema: Schema, body_parse_kwargs: dict[str, object]
    ) -> Query:
        """Scored F_BODY query for the content terms.

        Parsed into a boolean AST (:mod:`fnd.query_ast`) and lowered to Tantivy
        (:mod:`fnd.query_compile`), so wildcards (``crypto*``/``*tion``/``col?r``),
        ``term~N`` fuzzies, ``/regex/``, phrases, ``+``/``-`` and ``^`` boosts all
        compose inside ``AND``/``OR``/``NOT`` and parentheses. ``parse_query``
        (which drops ``*`` and no-ops ``~N``) is used only for plain term/phrase
        leaves, where it gives correct analyzer/stemming parity. Adjacency is a
        weighted OR (Should) so the bare-multi-term default still ranks all-term
        docs highest. A field-grouped clause (``title:(a OR b)``) left in the
        content by filter extraction is kept whole by the tokenizer and lowered
        through ``parse_query`` as one leaf, so wildcards/fuzzy beside it still
        compile.
        """
        import tantivy

        from fnd.query_ast import parse_query_ast
        from fnd.query_compile import compile_query

        node = parse_query_ast(content)
        if node is None:
            return tantivy.Query.empty_query()
        return compile_query(node, searcher=self, schema=schema, parse_kwargs=body_parse_kwargs)

    def _raw_hits(
        self,
        query: str,
        *,
        limit: int,
        collection: str | list[str] | None,
        active_sources: list[str] | None = None,
        fuzzy_distance: int = 0,
        intent: str | None = None,
        tag_filter: TagFilter | None = None,
    ) -> list[Hit]:
        import tantivy

        from fnd.query_dsl import preprocess
        from fnd.query_filters import extract_filters
        from fnd.schema import (
            F_BODY,
            F_COLLECTION,
            F_HEADING_PATH,
            F_PATH_TOKENS,
            F_SOURCE_PATH,
            build_schema,
        )
        from fnd.stopwords import strip_query_stopwords
        from fnd.tag_query import compile_tag_filter

        enforce_query_bounds(query)
        schema = build_schema()
        # Lower field/range/collection clauses into typed, unscored hard filters
        # (filter context) BEFORE proximity expansion: the registry parses raw
        # ``c:``/``mtime:today``/``page:>N`` forms directly, so a multi-collection
        # scope never becomes a re-parsed ``(a OR b)`` string. Proximity sugar
        # ({N}, NEAR/N) is then expanded on the residual content only.
        extracted = extract_filters(query, schema, self._index)
        # Drop standalone stopwords from the bag-of-words content so a chunk
        # matching only "and"/"in"/"the" (~zero IDF) isn't retrieved. Quoted
        # phrases and explicit-syntax queries pass through untouched.
        content = strip_query_stopwords(preprocess(extracted.content))
        filters = list(extracted.filters)
        # Tags are typed state, never query text — see fnd/tag_query.py.
        if tag_filter is not None and not tag_filter.is_empty():
            compiled_tags = compile_tag_filter(tag_filter, schema)
            if compiled_tags is not None:
                filters.append(compiled_tags)
        # Active collection (-c / settings) and source scope are hard filters.
        # ``collection`` accepts a single name (CLI ``-c``) or a list (the
        # TUI's multi-collection scope); a list becomes an OR over F_COLLECTION
        # terms so multi-collection scope HARD-restricts instead of riding a
        # re-parsed ``c:`` string that ranks softly and splits spaced names.
        # ``active_sources`` stays a SEPARATE filter, ANDed in: it narrows
        # WITHIN the collection (partial-source selection), not a union.
        if collection:
            cols = [collection] if isinstance(collection, str) else list(collection)
            col_terms = [tantivy.Query.term_query(schema, F_COLLECTION, c) for c in cols]
            filters.append(
                col_terms[0]
                if len(col_terms) == 1
                else tantivy.Query.boolean_query([(tantivy.Occur.Should, t) for t in col_terms])
            )
        if active_sources:
            src_terms = [tantivy.Query.term_query(schema, F_SOURCE_PATH, s) for s in active_sources]
            filters.append(
                src_terms[0]
                if len(src_terms) == 1
                else tantivy.Query.boolean_query([(tantivy.Occur.Should, t) for t in src_terms])
            )
        # tantivy-py's QueryParser doesn't honour ``term~N`` syntax for
        # tokenized fields, but it accepts a ``fuzzy_fields`` mapping
        # that auto-fuzzes every parsed term against the listed field.
        # ``(prefix, distance, transposition_cost_one)`` per term.
        body_parse_kwargs: dict[str, object] = {
            "default_field_names": [F_BODY],
        }
        if fuzzy_distance > 0:
            body_parse_kwargs["fuzzy_fields"] = {F_BODY: (False, fuzzy_distance, True)}
        # Must-clause: chunk's visible content (F_BODY) must match. A pure-filter
        # query (e.g. ``kind:pdf`` alone) has no content → match every chunk and
        # let the filters narrow. With neither content nor filters there is
        # nothing to match — return no results rather than the whole corpus.
        has_content = bool(content.strip())
        if has_content:
            body_required = self._content_body_query(content, schema, body_parse_kwargs)
        elif filters:
            body_required = tantivy.Query.all_query()
        else:
            body_required = tantivy.Query.empty_query()
        clauses: list[tuple[tantivy.Occur, tantivy.Query]] = [(tantivy.Occur.Must, body_required)]
        # Hard filters: required, but const-scored to 0 so they don't perturb BM25.
        for f in filters:
            clauses.append((tantivy.Occur.Must, tantivy.Query.const_score_query(f, 0.0)))
        # Should-clause: secondary fields boost score without gating visibility.
        # Parsed against the content (filters already removed). Skipped when the
        # content carries wildcard/fuzzy/regex tokens — parse_query can't handle
        # those (and the boost is best-effort, not a visibility gate). Each token
        # is stripped of ``+``/``-``/parens and a trailing ``^boost`` first so a
        # grouped / prefixed / boosted special token (``+crypto*``, ``(function~1)``,
        # ``/crypt(o|id)/^2``) is still detected. (``~N`` is KEPT — it's what makes
        # a fuzzy token special.)
        content_is_special = any(
            _WILDCARD_RE.match(c) or _FUZZY_RE.match(c) or _REGEX_RE.match(c) or _GLOB_RE.search(c)
            for t in content.split()
            for c in (re.sub(r"\^[\d.]+$", "", t.strip("+-()")),)
        )
        if has_content and not content_is_special:
            boost_secondary = _parse_query(
                self._index,
                content,
                default_field_names=[F_HEADING_PATH, F_TITLE, F_PATH_TOKENS],
                field_boosts={
                    F_HEADING_PATH: DEFAULT_FIELD_BOOSTS[F_HEADING_PATH],
                    F_TITLE: DEFAULT_FIELD_BOOSTS[F_TITLE],
                    F_PATH_TOKENS: DEFAULT_FIELD_BOOSTS[F_PATH_TOKENS],
                },
            )
            clauses.append((tantivy.Occur.Should, boost_secondary))
        parsed = tantivy.Query.boolean_query(clauses)
        # Pin one generation for the whole search→doc sequence. A
        # concurrent reload() may swap self._searcher mid-op; the
        # DocAddresses below are generation-specific, so reading them
        # against a newer searcher yields garbage (or a Rust panic).
        searcher = self._searcher
        result = searcher.search(parsed, limit=limit)

        from fnd.struct import decode as decode_body_struct

        out: list[Hit] = []
        for score, address in result.hits:
            doc = searcher.doc(address)
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
                    snippet=_make_snippet(body_text, query, intent=intent),
                    page_label=_first_str(doc, F_PAGE_LABEL),
                    chunk_seq=_first_int(doc, F_CHUNK_SEQ),
                    line=_first_int(doc, F_LINE),
                    mtime=_first_int(doc, F_MTIME),
                    meta_blob=meta_blob_bytes,
                    body_text=body_text,
                )
            )
        return out

    def _filtered_raw_hits(
        self,
        query: str,
        *,
        target: int,
        collection: str | list[str] | None,
        metadata_filter: str | None,
        active_sources: list[str] | None = None,
        fuzzy_distance: int = 0,
        intent: str | None = None,
        tag_filter: TagFilter | None = None,
    ) -> list[Hit]:
        """Return at least ``target`` hits, applying the optional metadata
        filter post-Tantivy with oversample-and-retry."""
        if metadata_filter is None:
            return self._raw_hits(
                query,
                limit=target,
                collection=collection,
                active_sources=active_sources,
                fuzzy_distance=fuzzy_distance,
                intent=intent,
                tag_filter=tag_filter,
            )
        from fnd.filter_dsl import compile_filter

        predicate = compile_filter(metadata_filter)
        oversample = 1
        max_oversample = 50
        while True:
            raw = self._raw_hits(
                query,
                limit=target * oversample,
                collection=collection,
                active_sources=active_sources,
                fuzzy_distance=fuzzy_distance,
                intent=intent,
                tag_filter=tag_filter,
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
        collection: str | list[str] | None = None,
        profile: object | None = None,
        now: int | None = None,
        metadata_filter: str | None = None,
        active_sources: list[str] | None = None,
        fuzzy_distance: int = 0,
        intent: str | None = None,
        tag_filter: TagFilter | None = None,
    ) -> list[Hit]:
        """Return one Hit per file (the file's best-scored chunk).

        The default ranking is unified on fusion (RRF), the same weighted
        ordering the TUI uses: a doc matching every query term outranks one
        matching only a single rarer term (raw BM25 over an OR does not
        guarantee that). The legacy single-pass BM25 path is kept only for the
        rerank ``profile`` (§4 recency / filetype / phrase-proximity) and the
        explicit cascade ``fuzzy_distance`` callers.

        Use :meth:`search_grouped` to keep all matched sections of each file.
        ``active_sources`` further narrows scope to chunks indexed from the
        listed source paths.
        """
        if not query.strip():
            return []
        if profile is None and fuzzy_distance == 0:
            from fnd.fusion import fusion_search

            fused = fusion_search(
                self,
                query=query,
                limit=limit * 5,  # oversample: per-file dedup below thins this
                collection=collection,
                metadata_filter=metadata_filter,
                active_sources=active_sources,
                intent=intent,
                tag_filter=tag_filter,
            )
            return _dedup_by_file(fused, limit)
        raw = self._filtered_raw_hits(
            query,
            target=limit * 5,
            collection=collection,
            metadata_filter=metadata_filter,
            active_sources=active_sources,
            fuzzy_distance=fuzzy_distance,
            intent=intent,
            tag_filter=tag_filter,
        )
        if profile is not None:
            from fnd.rerank import RankingProfile, rerank_hits

            assert isinstance(profile, RankingProfile)
            raw = rerank_hits(raw, profile=profile, query=query, now=now)
        return _dedup_by_file(raw, limit)

    def _decode_chunk(self, searcher: object, address: object) -> FileChunk:
        """Decode a single chunk's stored fields at ``address`` into a
        :class:`FileChunk`. Independent per-address — safe to call from
        a worker thread because ``Searcher.doc()`` releases the GIL
        inside tantivy. ``searcher`` is the generation-pinned view the
        caller searched against; passing it explicitly (rather than
        reading ``self._searcher``) keeps the address and the searcher on
        the same generation even if ``reload()`` swaps ``self._searcher``
        concurrently."""
        from fnd.struct import decode as decode_body_struct

        doc = searcher.doc(address)  # type: ignore[attr-defined]
        body_struct_bytes = doc.get_first(F_BODY_STRUCT)  # type: ignore[attr-defined]
        blocks = decode_body_struct(body_struct_bytes) if body_struct_bytes else []
        body_md_bytes = doc.get_first(F_BODY_MD)  # type: ignore[attr-defined]
        body_md = body_md_bytes.decode("utf-8") if body_md_bytes else ""
        return FileChunk(
            parent_id=_first_str(doc, F_PARENT_ID),
            path=_first_str(doc, F_PATH),
            kind=_first_str(doc, F_KIND),
            page=_first_int(doc, F_PAGE),
            slide=_first_int(doc, F_SLIDE),
            heading_path=_first_str(doc, F_HEADING_PATH),
            chunk_seq=_first_int(doc, F_CHUNK_SEQ),
            blocks=blocks,
            page_label=_first_str(doc, F_PAGE_LABEL),
            body_md=body_md,
        )

    def get_file_chunks(self, parent_id: str, *, max_workers: int | None = None) -> list[FileChunk]:
        """Return every indexed chunk of the file identified by ``parent_id``,
        ordered by ``chunk_seq``. Used by the TUI for the full-document preview.

        ``max_workers`` controls decode parallelism:

        * ``None`` (default) or ``<= 1``: serial decode — the historic
          behaviour, used by tests and by callers that don't want the
          thread-pool startup cost.
        * ``> 1`` *and* chunk count ≥ ``_PARALLEL_DECODE_THRESHOLD``:
          decode addresses concurrently via a :class:`ThreadPoolExecutor`.
          Tantivy releases the GIL inside ``Searcher.doc()`` so threads
          are the right primitive — multiprocessing would force schema /
          searcher serialisation per worker.
        * ``> 1`` and chunk count below the threshold: serial decode (the
          thread-pool overhead is not worth it).

        Implementation: query for ``parent_id:<id>`` with a wide limit
        then decode each chunk's stored body_struct via :meth:`_decode_chunk`.
        """
        parsed = self._index.parse_query(
            f'parent_id:"{parent_id}"',
            default_field_names=[F_PARENT_ID],
        )
        # 5000 chunks/file is a generous ceiling; phase 12 will revisit for
        # books / very long PDFs.
        # Pin one generation for the whole search→decode sequence: the
        # decode threads below dereference these DocAddresses, and a
        # concurrent reload() must not swap the searcher under them.
        searcher = self._searcher
        result = searcher.search(parsed, limit=5000)
        addresses = [address for _score, address in result.hits]
        workers = max_workers or 1
        if workers > 1 and len(addresses) >= _PARALLEL_DECODE_THRESHOLD:
            from concurrent.futures import ThreadPoolExecutor
            from functools import partial

            with ThreadPoolExecutor(max_workers=workers) as pool:
                chunks = list(pool.map(partial(self._decode_chunk, searcher), addresses))
        else:
            chunks = [self._decode_chunk(searcher, a) for a in addresses]
        chunks.sort(key=lambda c: c.chunk_seq)
        # A file listed under several collections (typical: an Obsidian
        # vault reachable via two nested source roots) is stored once per
        # collection — same parent_id, same content, distinct `collection`
        # field. The query above is scoped only by parent_id, so it sees
        # every collection's copy. Collapse to one chunk per chunk_seq so
        # the full-document preview renders each chunk once, not N times.
        seen: set[int] = set()
        deduped = []
        for c in chunks:
            if c.chunk_seq in seen:
                continue
            seen.add(c.chunk_seq)
            deduped.append(c)
        return deduped

    def search_grouped(
        self,
        query: str,
        *,
        limit: int = _DEFAULT_LIMIT,
        sections_per_file: int = 5,
        collection: str | list[str] | None = None,
        profile: object | None = None,
        now: int | None = None,
        metadata_filter: str | None = None,
        active_sources: list[str] | None = None,
        fuzzy_distance: int = 0,
        intent: str | None = None,
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
            fuzzy_distance=fuzzy_distance,
            intent=intent,
        )
        if profile is not None:
            from fnd.rerank import RankingProfile, rerank_hits

            assert isinstance(profile, RankingProfile)
            raw = rerank_hits(raw, profile=profile, query=query, now=now)
        return group_by_file(raw, limit=limit, sections_per_file=sections_per_file)


def group_by_file(
    hits: list[Hit],
    *,
    limit: int,
    sections_per_file: int = 5,
    score_threshold: float = 0.0,
) -> list[FileGroup]:
    """Bucket a flat ranked Hit list into per-file groups.

    Hits keep first-seen order, so passing pre-ranked output (BM25,
    reranked, fusion-fused, cascade-stitched) produces FileGroups in the
    same order. Sections are kept when the section's score is at least
    ``score_threshold * file_top_score`` and the per-file cap
    ``sections_per_file`` hasn't been hit yet; ``score_threshold = 0``
    disables the relative filter (cap-only behaviour).

    Used both by :meth:`Searcher.search_grouped` and the cascade /
    fusion paths in the TUI's ``_run_query`` so every search path
    funnels through identical grouping logic.
    """
    groups: dict[str, list[Hit]] = {}
    order: list[str] = []
    for h in hits:
        bucket = groups.get(h.parent_id)
        if bucket is None:
            groups[h.parent_id] = [h]
            order.append(h.parent_id)
        else:
            bucket.append(h)
    out: list[FileGroup] = []
    for pid in order[:limit]:
        all_hits = groups[pid]
        top = all_hits[0]
        # Relative-score filter: keep sections whose score is at least
        # ``threshold * top_score``. Threshold 0 disables (cap-only).
        if score_threshold > 0.0 and top.score > 0.0:
            min_score = top.score * score_threshold
            kept = [h for h in all_hits if h.score >= min_score]
        else:
            kept = all_hits
        section_hits = kept[:sections_per_file]
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
