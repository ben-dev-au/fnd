"""Cascading multi-pass query (§9c).

Three widening passes are tried in order:

  0. literal — query as the user typed it (already stem-aware via en_stem)
  1. fuzzy — Lucene-style "rewrite" fuzzy: enumerate the F_BODY term
     dictionary for indexed stems within the per-term auto-distance
     (0 for ≤2 chars, 1 for 3-5, 2 for ≥6 — same shape as Lucene's
     ``fuzziness=AUTO``), then issue an OR of regular ``term_query``
     for each matching stem. This is the same rewrite Lucene applies
     to ``MultiTermQuery`` so the matched docs land back on BM25
     scoring (TF/IDF/length) rather than the constant 1.0 Tantivy's
     ``fuzzy_term_query`` returns. A single-character prefix anchors
     the dictionary scan so a 6-char stem with distance 2 doesn't
     bring in unrelated short words.
  2. synonym — terms in any synonym group expand to (term OR sym1 OR sym2)

A pass runs only if the cumulative deduplicated hit count is below the
caller-supplied threshold. Hits surfaced first by an earlier pass keep
their original ``pass_index``; later-pass duplicates are discarded so the
TUI shows exact matches above fuzzy ones above synonym ones.
"""

from __future__ import annotations

import threading
from typing import Literal, overload

import snowballstemmer
import tantivy

from fnd.explain import CascadePassTrace, CascadeTrace
from fnd.matching import auto_fuzzy_distance, levenshtein_within
from fnd.query import Hit, Searcher
from fnd.render import _terms_from_query
from fnd.schema import F_BODY, F_META_BLOB, F_PAGE_LABEL, F_PARENT_ID, build_schema
from fnd.struct import decode as decode_body_struct
from fnd.synonyms import SynonymTable, expand

# ``F_BODY`` is analyzed with ``en_stem`` (Snowball English) at index
# time, so the on-disk token form for "Templates" is ``templat``. The
# fuzzy pass bypasses ``parse_query`` (which normally stems the query
# the same way), so we have to stem each query term ourselves before
# handing it to ``fuzzy_term_query`` — otherwise a 1-edit typo like
# "Templatas" → ``templatas`` ends up at distance 2 from the indexed
# ``templat`` and silently drops out of the cascade.
# threading.local: snowballstemmer instances aren't thread-safe.
_FUZZY_STEMMER_LOCAL = threading.local()


def _fuzzy_stem(term: str) -> str:
    s = getattr(_FUZZY_STEMMER_LOCAL, "instance", None)
    if s is None:
        s = snowballstemmer.stemmer("english")
        _FUZZY_STEMMER_LOCAL.instance = s
    return s.stemWord(term.lower())


# Cap on dictionary entries scanned per character bucket. F_BODY is
# en_stem-tokenised, so a typical English corpus has ~20-50k unique
# stems per leading character — the cap keeps the worst-case scan
# bounded on huge corpora without losing matches in normal ones.
_FUZZY_DICT_LIMIT = 50_000


def _fuzzy_term_variants(searcher: Searcher, stem: str, max_dist: int) -> list[str]:
    """Enumerate indexed F_BODY stems within ``max_dist`` of ``stem``.

    Walks the term dictionary (via ``Searcher.terms_with_prefix``) using
    the stem's first character as a prefix anchor — same trick Lucene's
    ``MultiTermQuery`` rewrite uses to keep the candidate set tight
    when distance ≥ 2. For ``max_dist == 0`` returns ``[stem]`` if
    indexed and ``[]`` otherwise. Returns the original stem first when
    present so the BooleanQuery's first sub-clause is the exact stem
    (matches Lucene's preference for the closest term).
    """
    if max_dist == 0:
        # Probe the dictionary cheaply: any prefix scan including this
        # stem returns it in the (term, count) list.
        return [
            t for t, _ in searcher._searcher.terms_with_prefix(F_BODY, stem, limit=1) if t == stem
        ]
    if not stem:
        return []
    # Anchor the scan to the first character — Lucene's rewrite uses a
    # single-char prefix when prefix_length isn't set; works because
    # any indexed term within edit distance N must share at least one
    # char with the query stem.
    prefix = stem[0]
    candidates = searcher._searcher.terms_with_prefix(F_BODY, prefix, limit=_FUZZY_DICT_LIMIT)
    out: list[str] = []
    seen = False
    for term, _count in candidates:
        if term == stem:
            seen = True
            continue
        if levenshtein_within(term, stem, max_dist=max_dist) <= max_dist:
            out.append(term)
    if seen:
        out.insert(0, stem)
    return out


def _fuzzy_pass(
    searcher: Searcher,
    *,
    query: str,
    limit: int,
    collection: str | None,
    active_sources: list[str] | None = None,
    intent: str | None = None,
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
    # ``F_BODY`` is en_stem-analyzed, so the on-disk token form for
    # "Templates" is ``templat``. The fuzzy pass bypasses parse_query
    # (and its query-time stemming), so we lowercase + Snowball-stem
    # each query term ourselves before consulting the dictionary —
    # otherwise the Levenshtein distance is computed between
    # mismatched token shapes (``templatas`` vs ``templat`` would
    # read as distance 2).
    #
    # We then expand each query stem into the set of indexed stems
    # within Lucene-AUTO edit distance and OR them as regular
    # ``term_query``s. This is the same rewrite Lucene applies to
    # ``MultiTermQuery`` so each matched doc lands on BM25 scoring
    # rather than Tantivy's constant-1.0 ``fuzzy_term_query`` output.
    stems = [_fuzzy_stem(t) for t in terms]
    distances = [auto_fuzzy_distance(s) for s in stems]
    subqueries: list[tuple[tantivy.Occur, tantivy.Query]] = []
    for stem, dist in zip(stems, distances, strict=True):
        variants = _fuzzy_term_variants(searcher, stem, dist)
        if not variants:
            # No indexed stem within distance — the AND of fuzzy term
            # clauses can never match, so bail early.
            return []
        if len(variants) == 1:
            subqueries.append(
                (
                    tantivy.Occur.Must,
                    tantivy.Query.term_query(schema, F_BODY, variants[0]),
                )
            )
        else:
            term_or = tantivy.Query.boolean_query(
                [
                    (tantivy.Occur.Should, tantivy.Query.term_query(schema, F_BODY, v))
                    for v in variants
                ]
            )
            subqueries.append((tantivy.Occur.Must, term_or))
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
        from fnd.schema import F_SOURCE_PATH

        source_subqueries: list[tuple[tantivy.Occur, tantivy.Query]] = [
            (tantivy.Occur.Should, tantivy.Query.term_query(schema, F_SOURCE_PATH, src))
            for src in active_sources
        ]
        subqueries.append((tantivy.Occur.Must, tantivy.Query.boolean_query(source_subqueries)))
    bq = tantivy.Query.boolean_query(subqueries)
    result = searcher._searcher.search(bq, limit=limit)
    return _materialize_hits(searcher, result.hits, query=query, intent=intent)


def _materialize_hits(
    searcher: Searcher,
    pairs: list[tuple[float, tantivy.DocAddress]],
    *,
    query: str,
    intent: str | None = None,
) -> list[Hit]:
    """Turn a (score, doc-address) list from a typed-API search into Hits.

    Pulled out of ``Searcher._raw_hits`` so the cascade can issue queries
    that bypass ``parse_query`` (e.g. the dictionary-rewritten fuzzy pass)
    but still yield the same Hit shape the rest of the system expects.
    """
    from fnd.query import _first_int, _first_str, _make_snippet  # local import: avoid cycle

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
                snippet=_make_snippet(body_text, query, intent=intent),
                page_label=_first_str(doc, F_PAGE_LABEL),
                chunk_seq=_first_int(doc, "chunk_seq"),
                mtime=_first_int(doc, "mtime"),
                meta_blob=meta_blob_bytes,
            )
        )
    return out


@overload
def cascade_search(
    searcher: Searcher,
    *,
    query: str,
    threshold: int,
    limit: int = ...,
    collection: str | None = ...,
    synonyms: SynonymTable | None = ...,
    metadata_filter: str | None = ...,
    active_sources: list[str] | None = ...,
    intent: str | None = ...,
    with_trace: Literal[False] = False,
) -> list[Hit]: ...


@overload
def cascade_search(
    searcher: Searcher,
    *,
    query: str,
    threshold: int,
    limit: int = ...,
    collection: str | None = ...,
    synonyms: SynonymTable | None = ...,
    metadata_filter: str | None = ...,
    active_sources: list[str] | None = ...,
    intent: str | None = ...,
    with_trace: Literal[True],
) -> tuple[list[Hit], CascadeTrace]: ...


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
    intent: str | None = None,
    with_trace: bool = False,
) -> list[Hit] | tuple[list[Hit], CascadeTrace]:
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

    ``with_trace`` (UX-pass-4 §2): when ``True``, returns
    ``(hits, CascadeTrace)`` so the layered search can format the
    regime label as ``cascade(+fuzzy)`` / ``cascade(+syn)`` based on
    which passes contributed new hits.
    """
    seen: set[tuple[str, int]] = set()
    out: list[Hit] = []
    pass_traces: list[CascadePassTrace] = []

    def _ingest(hits: list[Hit], pass_index: int) -> int:
        before = len(out)
        for h in hits:
            key = (h.parent_id, h.chunk_seq)
            if key in seen:
                continue
            seen.add(key)
            out.append(_with_pass(h, pass_index))
        return len(out) - before

    def _trace_result() -> tuple[list[Hit], CascadeTrace]:
        return out, CascadeTrace(
            query=query,
            passes=pass_traces,
            threshold=threshold,
            final_count=len(out),
        )

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
        intent=intent,
    )
    new_count = _ingest(raw, 0)
    if with_trace:
        pass_traces.append(
            CascadePassTrace(
                pass_index=0,
                name="literal",
                query=query,
                hit_count=len(raw),
                new_count=new_count,
                bm25_top=raw[0].score if raw else 0.0,
            )
        )
    if len(out) >= threshold:
        return _trace_result() if with_trace else out

    # Pass 1: fuzzy via typed API (text-syntax ~1 is not supported by
    # tantivy-py for indexed-non-fast text fields). Metadata filter is
    # applied post-hoc since the fuzzy pass bypasses parse_query entirely.
    fuzzy_raw = _fuzzy_pass(
        searcher,
        query=query,
        limit=pass_target,
        collection=collection,
        active_sources=active_sources,
        intent=intent,
    )
    fuzzy_raw = _apply_metadata_filter(fuzzy_raw, metadata_filter)
    new_count = _ingest(fuzzy_raw, 1)
    if with_trace:
        pass_traces.append(
            CascadePassTrace(
                pass_index=1,
                name="fuzzy",
                query=query,
                hit_count=len(fuzzy_raw),
                new_count=new_count,
                bm25_top=fuzzy_raw[0].score if fuzzy_raw else 0.0,
            )
        )
    if len(out) >= threshold:
        return _trace_result() if with_trace else out

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
                intent=intent,
            )
            new_count = _ingest(raw, 2)
            if with_trace:
                pass_traces.append(
                    CascadePassTrace(
                        pass_index=2,
                        name="synonym",
                        query=syn_q,
                        hit_count=len(raw),
                        new_count=new_count,
                        bm25_top=raw[0].score if raw else 0.0,
                    )
                )

    return _trace_result() if with_trace else out


def _apply_metadata_filter(hits: list[Hit], metadata_filter: str | None) -> list[Hit]:
    """Re-use the searcher's predicate logic on a list of hits produced
    outside ``parse_query`` (e.g. the fuzzy pass)."""
    if metadata_filter is None:
        return hits
    from fnd.filter_dsl import compile_filter
    from fnd.query import _passes_meta_filter

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
        page_label=h.page_label,
        chunk_seq=h.chunk_seq,
        mtime=h.mtime,
        pass_index=pass_index,
        meta_blob=h.meta_blob,
    )
