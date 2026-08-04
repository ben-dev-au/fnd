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

import dataclasses
import re
from typing import TYPE_CHECKING, Literal, overload

import tantivy

from fnd.explain import CascadePassTrace, CascadeTrace
from fnd.matching import auto_fuzzy_distance
from fnd.query import Hit, Searcher

if TYPE_CHECKING:
    from fnd.tag_query import TagFilter
from fnd.query_resolvers import fuzzy_stem as _fuzzy_stem
from fnd.query_resolvers import fuzzy_variants as _fuzzy_term_variants
from fnd.schema import F_BODY, F_META_BLOB, F_PAGE_LABEL, F_PARENT_ID, build_schema
from fnd.struct import decode as decode_body_struct
from fnd.synonyms import SynonymTable, expand


def _carries_precision_intent(query: str) -> bool:
    """True if the query expresses precision intent that the recall-widening
    fuzzy pass would violate, so the fuzzy pass must be skipped:

    * a quoted phrase, ``{N}`` proximity, or ``NEAR/N``;
    * a ``*``/``?`` wildcard (the fuzzy pass strips these and fuzzy-matches the
      bare stem — ``crypto*`` would re-admit ``cryptid``);
    * an explicit exclusion (``NOT x`` / ``-x``) — the fuzzy pass strips the
      operator and would re-admit the excluded docs.
    """
    if any(ch in query for ch in '"{*?') or "NEAR/" in query:
        return True
    # ``-word`` or ``-(group)`` exclusion (the ``(`` case would otherwise slip
    # past and the fuzzy pass would re-admit the excluded branch).
    return bool(re.search(r"\bNOT\b", query)) or bool(re.search(r"(?:^|\s)-[\w(]", query))


_FUZZY_TOKEN_RE = re.compile(r"^(\w+)(?:~(\d+)?)?$")
# Strip ``~N`` (and bare trailing ``~``) only when preceded by a word
# char — preserves phrase-proximity ``"a b"~3`` (~ after a quote).
_STRIP_FUZZY_MOD_RE = re.compile(r"(?<=\w)~\d*")


def _terms_with_fuzzy(query: str) -> list[tuple[str, int | None]]:
    """Like :func:`fnd.render._terms_from_query`, but preserves per-term
    ``~N`` modifiers.

    Returns ``(term, explicit_distance | None)`` tuples. Bare ``~`` with
    no digit reads as no modifier. ``~N`` is clamped to ``{1, 2}``.
    Quoted phrases are stripped — proximity (``"a b"~3``) isn't fuzzy.
    """
    if not query:
        return []
    q = re.sub(r'"[^"]*"', " ", query)  # drop phrases (proximity ≠ fuzzy)
    q = re.sub(r"\[[^\]]*\]", " ", q)
    q = re.sub(r"\{\d+\}", " ", q)
    q = re.sub(r"\bNEAR/\d+\b", " ", q)
    q = re.sub(r"\b\w+:\([^)]*\)", " ", q)  # field grouping: title:(a OR b)
    q = re.sub(r"\b\w+:\S+", " ", q)
    q = re.sub(r"[+\-()*?]", " ", q)
    q = re.sub(r"\b(AND|OR|NOT)\b", " ", q)
    out: list[tuple[str, int | None]] = []
    for tok in q.split():
        m = _FUZZY_TOKEN_RE.match(tok)
        if not m:
            continue
        term = m.group(1)
        dist_str = m.group(2)
        if dist_str is None or dist_str == "":
            out.append((term, None))
        else:
            out.append((term, min(int(dist_str), 2)))
    return out


def _strip_fuzzy_modifiers(query: str) -> str:
    """Remove ``~N`` / bare ``~`` after a word char. Used to clean the
    query before the literal + synonym passes, which submit through
    tantivy's QueryParser (the parser silently no-ops ``~N`` for
    indexed-non-fast body fields; we strip to make the literal probe
    look like the user's intended exact match)."""
    return _STRIP_FUZZY_MOD_RE.sub("", query)


def _fuzzy_pass(
    searcher: Searcher,
    *,
    query: str,
    limit: int,
    collection: str | list[str] | None,
    active_sources: list[str] | None = None,
    intent: str | None = None,
    auto_fuzzy_enabled: bool = True,
    min_term_chars: int = 0,
    tag_filter: TagFilter | None = None,
) -> list[Hit]:
    """Build a Boolean query of fuzzy term queries over the body field,
    AND-combined so all query terms must fuzzy-match.

    ``auto_fuzzy_enabled`` gates the AUTO-distance heuristic. When
    False, only terms with an explicit ``~N`` modifier in the query
    get expanded; everything else resolves to distance 0. If every
    term resolves to 0, the pass returns ``[]`` (pass-0 already
    covered the exact case).

    ``min_term_chars`` is the post-stem length floor for auto-fuzzy.
    Stems shorter than this skip auto-fuzzy regardless of the AUTO
    heuristic. Per-term ``~N`` overrides the floor.

    ``active_sources`` further narrows the fuzzy pass to chunks indexed
    from a subset of the active collection's sources, so the §9c cascade
    fallback honours the same source-scope as the literal pass.
    """
    term_dists = _terms_with_fuzzy(query)
    if not term_dists:
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
    # within edit distance and OR them as regular ``term_query``s.
    # This is the same rewrite Lucene applies to ``MultiTermQuery``
    # so each matched doc lands on BM25 scoring rather than Tantivy's
    # constant-1.0 ``fuzzy_term_query`` output.
    stems_with_dists: list[tuple[str, int]] = []
    for term, explicit in term_dists:
        stem = _fuzzy_stem(term)
        if explicit is not None:
            d = explicit
        elif auto_fuzzy_enabled and len(stem) >= min_term_chars:
            d = auto_fuzzy_distance(stem)
        else:
            d = 0
        stems_with_dists.append((stem, d))
    if all(d == 0 for _, d in stems_with_dists):
        return []
    subqueries: list[tuple[tantivy.Occur, tantivy.Query]] = []
    for stem, dist in stems_with_dists:
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
        # Restrict to a collection (or, for the TUI's multi-collection scope,
        # ANY of a list) on the ``collection`` field. Const-scored to 0 so it's
        # a pure hard filter — without it a multi-collection OR lets per-
        # collection IDF skew BM25 between the selected collections (matches
        # the unscored hard-filter handling in ``query.py::_raw_hits``).
        cols = [collection] if isinstance(collection, str) else list(collection)
        col_terms = [tantivy.Query.term_query(schema, "collection", c) for c in cols]
        col_q = (
            col_terms[0]
            if len(col_terms) == 1
            else tantivy.Query.boolean_query([(tantivy.Occur.Should, t) for t in col_terms])
        )
        subqueries.append((tantivy.Occur.Must, tantivy.Query.const_score_query(col_q, 0.0)))
    if active_sources:
        # Active source-set filter, ANDed within the collection scope above
        # (not unioned). Const-scored for the same reason as the collection
        # filter: source-path IDF must not perturb ranking.
        from fnd.schema import F_SOURCE_PATH

        src_terms = [tantivy.Query.term_query(schema, F_SOURCE_PATH, src) for src in active_sources]
        src_q = (
            src_terms[0]
            if len(src_terms) == 1
            else tantivy.Query.boolean_query([(tantivy.Occur.Should, t) for t in src_terms])
        )
        subqueries.append((tantivy.Occur.Must, tantivy.Query.const_score_query(src_q, 0.0)))
    # Apply the same field/range/collection hard filters as the literal pass, so
    # widening to fuzzy can't leak docs the user's qualifiers excluded.
    from fnd.query_filters import extract_filters

    for filt in extract_filters(query, schema, searcher._index).filters:
        subqueries.append((tantivy.Occur.Must, tantivy.Query.const_score_query(filt, 0.0)))
    # Tags are typed state rather than query text, so extract_filters can't
    # see them; without this the fuzzy pass re-admits tag-excluded files.
    if tag_filter is not None and not tag_filter.is_empty():
        from fnd.tag_query import compile_tag_filter

        compiled_tags = compile_tag_filter(tag_filter, schema)
        if compiled_tags is not None:
            subqueries.append(
                (tantivy.Occur.Must, tantivy.Query.const_score_query(compiled_tags, 0.0))
            )
    bq = tantivy.Query.boolean_query(subqueries)
    # Pin one searcher generation for the whole search→doc sequence so a
    # concurrent reload() can't swap it between the search and materialisation
    # (the same guard _raw_hits uses against cross-generation DocAddresses).
    searcher_view = searcher._searcher
    result = searcher_view.search(bq, limit=limit)
    return _materialize_hits(searcher_view, result.hits, query=query, intent=intent)


def _materialize_hits(
    searcher_view: object,
    pairs: list[tuple[float, tantivy.DocAddress]],
    *,
    query: str,
    intent: str | None = None,
) -> list[Hit]:
    """Turn a (score, doc-address) list from a typed-API search into Hits.

    Pulled out of ``Searcher._raw_hits`` so the cascade can issue queries
    that bypass ``parse_query`` (e.g. the dictionary-rewritten fuzzy pass)
    but still yield the same Hit shape the rest of the system expects.
    ``searcher_view`` is the generation-pinned snapshot the caller searched
    against — addresses must be dereferenced on the same generation.
    """
    from fnd.query import _first_int, _first_str, _make_snippet  # local import: avoid cycle

    out: list[Hit] = []
    for score, address in pairs:
        doc = searcher_view.doc(address)  # type: ignore[attr-defined]
        body_struct_bytes = doc.get_first("body_struct")  # type: ignore[attr-defined]
        body_text = ""
        if body_struct_bytes is not None:
            blocks = decode_body_struct(body_struct_bytes)
            body_text = "\n".join(b.text for b in blocks)
        meta_blob_bytes = doc.get_first(F_META_BLOB)  # type: ignore[attr-defined]
        if meta_blob_bytes is None:
            meta_blob_bytes = b""
        body_md_bytes = doc.get_first("body_md")  # type: ignore[attr-defined]
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
                line=_first_int(doc, "line"),
                mtime=_first_int(doc, "mtime"),
                meta_blob=meta_blob_bytes,
                body_text=body_text,
                body_md=body_md_bytes.decode("utf-8") if body_md_bytes else "",
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
    collection: str | list[str] | None = ...,
    synonyms: SynonymTable | None = ...,
    metadata_filter: str | None = ...,
    active_sources: list[str] | None = ...,
    intent: str | None = ...,
    auto_fuzzy_enabled: bool = ...,
    min_term_chars: int = ...,
    tag_filter: TagFilter | None = ...,
    with_trace: Literal[False] = False,
) -> list[Hit]: ...


@overload
def cascade_search(
    searcher: Searcher,
    *,
    query: str,
    threshold: int,
    limit: int = ...,
    collection: str | list[str] | None = ...,
    synonyms: SynonymTable | None = ...,
    metadata_filter: str | None = ...,
    active_sources: list[str] | None = ...,
    intent: str | None = ...,
    auto_fuzzy_enabled: bool = ...,
    min_term_chars: int = ...,
    tag_filter: TagFilter | None = ...,
    with_trace: Literal[True],
) -> tuple[list[Hit], CascadeTrace]: ...


def cascade_search(
    searcher: Searcher,
    *,
    query: str,
    threshold: int,
    limit: int = 50,
    collection: str | list[str] | None = None,
    synonyms: SynonymTable | None = None,
    metadata_filter: str | None = None,
    active_sources: list[str] | None = None,
    intent: str | None = None,
    auto_fuzzy_enabled: bool = True,
    min_term_chars: int = 0,
    tag_filter: TagFilter | None = None,
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

    # Literal + synonym passes go through tantivy's QueryParser, which
    # silently no-ops ``~N`` on indexed-non-fast body fields. Strip the
    # modifiers so those passes see the user's intended exact spelling;
    # the fuzzy pass (below) reads them off the original query.
    literal_query = _strip_fuzzy_modifiers(query)

    # Pass 0: literal query through the standard parse_query path.
    raw = searcher._filtered_raw_hits(
        literal_query,
        target=pass_target,
        collection=collection,
        metadata_filter=metadata_filter,
        active_sources=active_sources,
        intent=intent,
        tag_filter=tag_filter,
    )
    new_count = _ingest(raw, 0)
    if with_trace:
        pass_traces.append(
            CascadePassTrace(
                pass_index=0,
                name="literal",
                query=literal_query,
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
    # Skipped for phrase/proximity queries: those express precision intent, and
    # the fuzzy pass strips proximity ({N}/NEAR) and would re-admit far matches.
    fuzzy_raw = (
        []
        if _carries_precision_intent(query)
        else _fuzzy_pass(
            searcher,
            query=query,
            limit=pass_target,
            collection=collection,
            active_sources=active_sources,
            intent=intent,
            auto_fuzzy_enabled=auto_fuzzy_enabled,
            min_term_chars=min_term_chars,
            tag_filter=tag_filter,
        )
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

    # Pass 2: synonym expansion through parse_query. Skipped for precision-intent
    # queries (same as the fuzzy pass): ``expand`` grafts an ``(a OR b)``
    # disjunction in, which strands a proximity brace (``{20}("a b" OR c)``) and
    # Tantivy rejects it.
    if synonyms is not None and synonyms.groups and not _carries_precision_intent(query):
        syn_q = expand(literal_query, synonyms)
        if syn_q != literal_query:
            raw = searcher._filtered_raw_hits(
                syn_q,
                target=pass_target,
                collection=collection,
                metadata_filter=metadata_filter,
                active_sources=active_sources,
                intent=intent,
                tag_filter=tag_filter,
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
    dataclasses, so we copy rather than mutate — via ``dataclasses.replace``,
    which cannot drop a field the way an enumerated rebuild does (see
    :func:`fnd.fusion._with_score`)."""
    return dataclasses.replace(h, pass_index=pass_index)
