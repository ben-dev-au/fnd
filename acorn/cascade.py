"""Cascading multi-pass query (§9c).

Three widening passes are tried in order:

  0. literal — query as the user typed it (already stem-aware via en_stem)
  1. fuzzy — every bare word becomes a ``fuzzy_term_query(distance=1)``
     against the body field (Tantivy's ``parse_query`` does NOT support the
     ``term~1`` text syntax, so we build fuzzy queries via the typed API
     instead — verified against tantivy-py 0.26)
  2. synonym — terms in any synonym group expand to (term OR sym1 OR sym2)

A pass runs only if the cumulative deduplicated hit count is below the
caller-supplied threshold. Hits surfaced first by an earlier pass keep
their original ``pass_index``; later-pass duplicates are discarded so the
TUI shows exact matches above fuzzy ones above synonym ones.
"""

from __future__ import annotations

import tantivy

from acorn.query import Hit, Searcher
from acorn.render import _terms_from_query
from acorn.schema import F_BODY, F_META_BLOB, F_PARENT_ID, build_schema
from acorn.struct import decode as decode_body_struct
from acorn.synonyms import SynonymTable, expand


def _fuzzy_pass(
    searcher: Searcher,
    *,
    query: str,
    limit: int,
    collection: str | None,
    active_sources: list[str] | None = None,
) -> list[Hit]:
    """Build a Boolean query of fuzzy term queries (distance=1) over the
    body field, AND-combined so all query terms must fuzzy-match.

    Returns nothing for queries with no plain word terms (operators-only).
    ``active_sources`` further narrows the fuzzy pass to chunks indexed
    from a subset of the active collection's sources, so the §9c cascade
    fallback honours the same source-scope as the literal pass.
    """
    terms = _terms_from_query(query)
    if not terms:
        return []
    schema = build_schema()
    # ``fuzzy_term_query`` is case-sensitive and operates on the indexed
    # token form. Body uses ``en_stem`` (lowercased + Snowball stem), so
    # we lowercase the user-typed term before issuing the fuzzy query.
    subqueries: list[tuple[tantivy.Occur, tantivy.Query]] = [
        (
            tantivy.Occur.Must,
            tantivy.Query.fuzzy_term_query(schema, F_BODY, t.lower(), distance=1),
        )
        for t in terms
    ]
    if collection:
        # Restrict to a collection by AND'ing a term query on the
        # ``collection`` field.
        subqueries.append(
            (
                tantivy.Occur.Must,
                tantivy.Query.term_query(schema, "collection", collection),
            )
        )
    if active_sources:
        # Active source-set filter: a Should-OR group inside a Must
        # bucket so any one source path matching satisfies the clause.
        from acorn.schema import F_SOURCE_PATH

        source_subqueries: list[tuple[tantivy.Occur, tantivy.Query]] = [
            (tantivy.Occur.Should, tantivy.Query.term_query(schema, F_SOURCE_PATH, src))
            for src in active_sources
        ]
        subqueries.append((tantivy.Occur.Must, tantivy.Query.boolean_query(source_subqueries)))
    bq = tantivy.Query.boolean_query(subqueries)
    result = searcher._searcher.search(bq, limit=limit)
    return _materialize_hits(searcher, result.hits, query=query)


def _materialize_hits(
    searcher: Searcher,
    pairs: list[tuple[float, tantivy.DocAddress]],
    *,
    query: str,
) -> list[Hit]:
    """Turn a (score, doc-address) list from a typed-API search into Hits.

    Pulled out of ``Searcher._raw_hits`` so the cascade can issue queries
    that bypass ``parse_query`` (e.g. fuzzy_term_query) but still yield the
    same Hit shape the rest of the system expects.
    """
    from acorn.query import _first_int, _first_str, _make_snippet  # local import: avoid cycle

    out: list[Hit] = []
    for score, address in pairs:
        doc = searcher._searcher.doc(address)
        body_struct_bytes = doc.get_first("body_struct")  # type: ignore[attr-defined]
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
                path=_first_str(doc, "path"),
                kind=_first_str(doc, "kind"),
                page=_first_int(doc, "page"),
                slide=_first_int(doc, "slide"),
                heading_path=_first_str(doc, "heading_path"),
                title=_first_str(doc, "title"),
                snippet=_make_snippet(body_text, query),
                chunk_seq=_first_int(doc, "chunk_seq"),
                mtime=_first_int(doc, "mtime"),
                meta_blob=meta_blob_bytes,
            )
        )
    return out


def cascade_search(
    searcher: Searcher,
    *,
    query: str,
    threshold: int,
    limit: int = 50,
    collection: str | None = None,
    synonyms: SynonymTable | None = None,
    metadata_filter: str | None = None,
    active_sources: list[str] | None = None,
) -> list[Hit]:
    """Run literal → fuzzy → synonym passes until ``threshold`` hits found.

    Returns hits with :attr:`Hit.pass_index` set to the pass that first
    surfaced each one (0=literal, 1=fuzzy, 2=synonym). Order: pass-0 hits
    in original score order, then pass-1, then pass-2 — so the TUI shows
    exact matches above looser matches.

    ``metadata_filter`` and ``active_sources`` apply to every pass so
    cascade preserves the same scope a single-pass search would, even
    when widening to fuzzy / synonym. Literal + synonym passes go through
    :meth:`Searcher._filtered_raw_hits` (which honours the metadata
    filter); the programmatic fuzzy pass adds an inline source-set
    clause to its boolean query.
    """
    seen: set[tuple[str, int]] = set()
    out: list[Hit] = []

    def _ingest(hits: list[Hit], pass_index: int) -> None:
        for h in hits:
            key = (h.parent_id, h.chunk_seq)
            if key in seen:
                continue
            seen.add(key)
            out.append(_with_pass(h, pass_index))

    # Oversample so the caller's per-file grouper has enough chunks to
    # bucket into ``limit`` files. Mirrors the ``target = limit * 10``
    # contract Searcher.search_grouped used.
    pass_target = limit * 10

    # Pass 0: literal query through the standard parse_query path.
    raw = searcher._filtered_raw_hits(
        query,
        target=pass_target,
        collection=collection,
        metadata_filter=metadata_filter,
        active_sources=active_sources,
    )
    _ingest(raw, 0)
    if len(out) >= threshold:
        return out

    # Pass 1: fuzzy via typed API (text-syntax ~1 is not supported by
    # tantivy-py for indexed-non-fast text fields). Metadata filter is
    # applied post-hoc since the fuzzy pass bypasses parse_query entirely.
    fuzzy_raw = _fuzzy_pass(
        searcher,
        query=query,
        limit=pass_target,
        collection=collection,
        active_sources=active_sources,
    )
    fuzzy_raw = _apply_metadata_filter(fuzzy_raw, metadata_filter)
    _ingest(fuzzy_raw, 1)
    if len(out) >= threshold:
        return out

    # Pass 2: synonym expansion through parse_query.
    if synonyms is not None and synonyms.groups:
        syn_q = expand(query, synonyms)
        if syn_q != query:
            raw = searcher._filtered_raw_hits(
                syn_q,
                target=pass_target,
                collection=collection,
                metadata_filter=metadata_filter,
                active_sources=active_sources,
            )
            _ingest(raw, 2)

    return out


def _apply_metadata_filter(hits: list[Hit], metadata_filter: str | None) -> list[Hit]:
    """Re-use the searcher's predicate logic on a list of hits produced
    outside ``parse_query`` (e.g. the fuzzy pass)."""
    if metadata_filter is None:
        return hits
    from acorn.filter_dsl import compile_filter
    from acorn.query import _passes_meta_filter

    predicate = compile_filter(metadata_filter)
    return [h for h in hits if _passes_meta_filter(h, predicate)]


def _with_pass(h: Hit, pass_index: int) -> Hit:
    """Return a copy of ``h`` tagged with ``pass_index``. Hits are frozen
    dataclasses, so we rebuild rather than mutate."""
    return Hit(
        score=h.score,
        parent_id=h.parent_id,
        path=h.path,
        kind=h.kind,
        page=h.page,
        slide=h.slide,
        heading_path=h.heading_path,
        title=h.title,
        snippet=h.snippet,
        chunk_seq=h.chunk_seq,
        mtime=h.mtime,
        pass_index=pass_index,
        meta_blob=h.meta_blob,
    )
