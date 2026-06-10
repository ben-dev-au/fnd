"""Query-language acceptance spec (feat/query-engine-rework).

Encodes the desired end-state behaviour for every capability the README claims,
verified against BOTH live search paths:

  * Searcher.search   — single-pass (CLI `fnd search`)
  * search_layered    — probe→fusion→cascade (TUI / --explain)

Rows that pass today are regression guards. Rows that are broken today are marked
``xfail(strict=True)`` with the phase that will fix them — flip the marker to a plain
test as each phase lands (a strict xfail that starts passing fails the suite, so we
can't forget). See dev/audits/QUERY_SYNTAX_AUDIT.md.

This is a *query-layer* contract: the index is built directly against ``build_schema``
with controlled field values, so behaviour is deterministic and covers fields
(page/slide/kind/collection/mtime) that real md/txt fixtures can't exercise. Indexing
itself is covered by test_index_query_roundtrip.
"""

from __future__ import annotations

import datetime as dt
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import TypedDict

import pytest
import tantivy

from fnd import meta_blob, struct
from fnd.extract.base import Block
from fnd.layered import search_layered
from fnd.query import Searcher
from fnd.query_plan import QueryPlan
from fnd.schema import (
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
    F_SOURCE_PATH,
    F_TITLE,
    SCHEMA_VERSION,
    build_schema,
)

# Real clock so the mtime token windows (today/year, evaluated at query time
# against the real clock) line up with the corpus stamps.
_NOW = int(dt.datetime.now(dt.UTC).timestamp())
_DAY = 86_400


def _ago(days: int) -> int:
    return _NOW - days * _DAY


def _iso(s: str) -> int:
    d = dt.date.fromisoformat(s)
    return int(dt.datetime(d.year, d.month, d.day, tzinfo=dt.UTC).timestamp())


class _DocBase(TypedDict):
    body: str


class _Doc(_DocBase, total=False):
    title: str
    heading_path: str
    author: str
    kind: str
    collection: str
    path: str
    path_tokens: str
    mtime: int
    page: int
    slide: int
    fm: dict[str, object]


# parent_id is the recognisable tag (raw field, not in any default search field).
_DOCS: list[tuple[str, _Doc]] = [
    ("stem-sing", {"body": "entropy measures disorder in a system"}),
    ("stem-plur", {"body": "the entropies of several systems were tabulated"}),
    ("all3", {"body": "cross entropy loss is a standard objective"}),
    ("cross-only", {"body": "cross validation splits the dataset"}),
    ("entropy-only", {"body": "entropy alone appears in this passage"}),
    ("phrase-ok", {"body": "we used cross entropy loss to train models"}),
    ("phrase-hyphen", {"body": "a cross-entropy loss term was added"}),
    ("phrase-reversed", {"body": "loss of entropy across the cross section"}),
    ("has-reg", {"body": "entropy and regression were both discussed"}),
    ("prox-near", {"body": "cross and then immediately entropy follows here"}),
    ("prox-far", {"body": "cross " + "filler " * 40 + "entropy"}),
    ("fuzzy-mito", {"body": "the mitochondria is the powerhouse of the cell"}),
    ("fuzzy-kube", {"body": "kubernetes orchestrates containers at scale"}),
    (
        "fld-title",
        {"body": "neural networks process sequences", "title": "transformer architecture"},
    ),
    (
        "fld-heading",
        {"body": "the proof proceeds by induction", "heading_path": "Chapter 4 / Proofs"},
    ),
    ("fld-author", {"body": "structured programming notes", "author": "Edsger Dijkstra"}),
    (
        "fld-path",
        {
            "body": "final results section",
            "path": "/home/u/thesis/final.pdf",
            "path_tokens": "home u thesis final pdf",
        },
    ),
    ("kind-pdf", {"body": "diffusion model overview", "kind": "pdf"}),
    ("kind-docx", {"body": "diffusion model overview", "kind": "docx"}),
    ("kind-md", {"body": "diffusion model overview", "kind": "md"}),
    ("col-wine", {"body": "attack on the cellar", "collection": "wine"}),
    ("col-papers", {"body": "attack surface analysis", "collection": "papers"}),
    ("pg-5", {"body": "content located on a page", "kind": "pdf", "page": 5}),
    ("pg-15", {"body": "content located mid document", "kind": "pdf", "page": 15}),
    ("pg-25", {"body": "content located on a later page", "kind": "pdf", "page": 25}),
    ("sl-3", {"body": "early slide material", "kind": "pptx", "slide": 3}),
    ("sl-12", {"body": "late slide material attention mechanism", "kind": "pptx", "slide": 12}),
    ("mt-today", {"body": "edited very recently corpustoken", "mtime": _ago(0)}),
    ("mt-old", {"body": "edited long ago corpustoken", "mtime": _iso("2020-01-01")}),
    ("mt-2024h1", {"body": "edited early twenty four corpustoken", "mtime": _iso("2024-03-15")}),
    ("wc-crypto", {"body": "crypto wallet basics"}),
    ("wc-graphy", {"body": "cryptography protects messages"}),
    ("wc-graphic", {"body": "a cryptographic hash function"}),
    ("wc-other", {"body": "cryptids are mythical creatures"}),
    (
        "fm-sec-lec",
        {
            "body": "mitm replay attack notes",
            "kind": "md",
            "fm": {
                "Course": "Security Foundations",
                "Year": 2024,
                "Notes_Type": "Lecture",
                "Tags": ["draft-1"],
            },
        },
    ),
    (
        "fm-ml-tut",
        {
            "body": "gradient descent corpustoken",
            "kind": "md",
            "fm": {"Course": "ML", "Year": 2023, "Notes_Type": "Tutorial", "Tags": ["final"]},
        },
    ),
]


@pytest.fixture(autouse=True)
def _frozen_clock(monkeypatch: pytest.MonkeyPatch) -> None:  # pyright: ignore[reportUnusedFunction]
    """Pin the mtime-resolution clock to the same instant the corpus was stamped
    against (``_NOW``). Otherwise ``mtime:today`` resolves against the live clock
    and a run that straddles UTC midnight flips ``mt-today`` out of the bucket."""
    monkeypatch.setattr("fnd.query_fields._now_ts", lambda: _NOW)
    monkeypatch.setattr("fnd.query_dsl._now_ts", lambda: _NOW)


@pytest.fixture(scope="module")
def searcher() -> Searcher:
    schema = build_schema()
    d = Path(tempfile.mkdtemp(prefix="fnd-acceptance-"))
    idx = tantivy.Index(schema, path=str(d))
    w = idx.writer()
    for pid, f in _DOCS:
        doc = tantivy.Document()
        doc.add_text(F_PARENT_ID, pid)
        doc.add_text(F_BODY, f["body"])
        doc.add_text(F_TITLE, f.get("title", ""))
        doc.add_text(F_HEADING_PATH, f.get("heading_path", ""))
        doc.add_text(F_AUTHOR, f.get("author", ""))
        doc.add_text(F_KIND, f.get("kind", "txt"))
        doc.add_text(F_COLLECTION, f.get("collection", "default"))
        doc.add_text(F_SOURCE_PATH, "/src")
        doc.add_text(F_PATH, f.get("path", f"/docs/{pid}.txt"))
        doc.add_text(F_PATH_TOKENS, f.get("path_tokens", f"docs {pid} txt"))
        doc.add_unsigned(F_MTIME, f.get("mtime", _ago(10)))
        doc.add_unsigned(F_PAGE, f.get("page", 0))
        doc.add_unsigned(F_SLIDE, f.get("slide", 0))
        doc.add_unsigned(F_CHUNK_SEQ, 0)
        doc.add_bytes(F_BODY_STRUCT, struct.encode([Block(kind="paragraph", text=f["body"])]))
        doc.add_bytes(F_META_BLOB, meta_blob.encode(f["fm"]) if "fm" in f else b"")
        w.add_document(doc)
    w.commit()
    idx.reload()
    (d / ".fnd-schema-version").write_text(str(SCHEMA_VERSION))
    return Searcher(index_dir=d)


def _single(s: Searcher, q: str) -> set[str]:
    p = QueryPlan.from_user_text(q)
    return {h.parent_id for h in s.search(p.lexical, limit=50, metadata_filter=p.metadata_filter)}


def _layered(s: Searcher, q: str) -> set[str]:
    p = QueryPlan.from_user_text(q)
    groups = search_layered(
        s, query=p.lexical, limit=50, sections_per_file=5, metadata_filter=p.metadata_filter
    )
    return {h.parent_id for g in groups for h in g.hits}


# (id, query, predicate, broken_paths). ``broken_paths`` lists the paths that
# are expected to FAIL today (strict xfail) — empty means "must pass on both".
# As each phase lands, shrink the set; a strict xfail that starts passing fails
# the suite, so a fixed capability can't silently stay marked broken.
type _Case = tuple[str, str, Callable[[set[str]], bool], frozenset[str]]
_OK: frozenset[str] = frozenset()
_CASES: list[_Case] = [
    ("stemming", "entropy", lambda r: {"stem-sing", "stem-plur"} <= r, _OK),
    (
        "phrase-order",
        '"cross entropy loss"',
        lambda r: "phrase-ok" in r and "phrase-reversed" not in r and "cross-only" not in r,
        _OK,
    ),
    ("phrase-hyphen", '"cross entropy"', lambda r: "phrase-hyphen" in r, _OK),
    ("bool-or", "crossxyz OR entropy", lambda r: "entropy-only" in r and "stem-sing" in r, _OK),
    ("bool-not", "entropy NOT regression", lambda r: "stem-sing" in r and "has-reg" not in r, _OK),
    ("bool-explicit-and", "cross AND loss", lambda r: "all3" in r and "cross-only" not in r, _OK),
    (
        "parens-group",
        "(crossxyz OR entropy) AND loss",
        lambda r: "all3" in r and "entropy-only" not in r,
        _OK,
    ),
    (
        "parens-nested",
        "(cross AND loss) OR regression",
        lambda r: "all3" in r and "has-reg" in r and "cross-only" not in r,
        _OK,
    ),
    ("path-tokens", "path_tokens:thesis", lambda r: "fld-path" in r, _OK),
    ("heading-path", "heading_path:proofs", lambda r: "fld-heading" in r, _OK),
    # weighted-default ranking is order-sensitive — see test_weighted_default_ranking_*.
    # --- P2: filter context + ranges (now live) ---
    ("filter-kind", "kind:docx diffusion", lambda r: r == {"kind-docx"}, _OK),
    (
        # Hard filter: title:transformer AND body:networks → only the doc with
        # both. A title qualifier must not OR-in body-only matches.
        "filter-title-hard",
        "title:transformer networks",
        lambda r: r == {"fld-title"},
        _OK,
    ),
    (
        "filter-collection",
        "c:wine attack",
        lambda r: "col-wine" in r and "col-papers" not in r,
        _OK,
    ),
    (
        "filter-collection-multi",
        "c:wine,papers attack",
        lambda r: {"col-wine", "col-papers"} <= r,
        _OK,
    ),
    (
        "filter-page-exact",
        "page:5 content",
        lambda r: "pg-5" in r and "pg-15" not in r and "pg-25" not in r,
        _OK,
    ),
    ("filter-page-gt", "page:>20 content", lambda r: "pg-25" in r and "pg-5" not in r, _OK),
    (
        "filter-page-range",
        "page:[10 TO 20] content",
        lambda r: "pg-15" in r and "pg-5" not in r and "pg-25" not in r,
        _OK,
    ),
    ("filter-slide-lt", "slide:<5 material", lambda r: "sl-3" in r and "sl-12" not in r, _OK),
    (
        "filter-mtime-today",
        "mtime:today corpustoken",
        lambda r: "mt-today" in r and "mt-old" not in r and "mt-2024h1" not in r,
        _OK,
    ),
    (
        "filter-mtime-iso-gt",
        "mtime:>2024-01-01 corpustoken",
        lambda r: "mt-2024h1" in r and "mt-old" not in r,
        _OK,
    ),
    (
        "filter-mtime-iso-range",
        "mtime:[2024-01-01 TO 2024-06-30] corpustoken",
        lambda r: "mt-2024h1" in r and "mt-today" not in r and "mt-old" not in r,
        _OK,
    ),
    (
        "proximity-brace",
        "{6} cross entropy",
        lambda r: "prox-near" in r and "prox-far" not in r,
        _OK,
    ),
    (
        "proximity-near",
        "cross NEAR/6 entropy",
        lambda r: "prox-near" in r and "prox-far" not in r,
        _OK,
    ),
    # --- P3: wildcard + fuzzy (now live on both paths) ---
    (
        "wildcard-prefix",
        "crypto*",
        lambda r: {"wc-crypto", "wc-graphy", "wc-graphic"} <= r and "wc-other" not in r,
        _OK,
    ),
    ("fuzzy-transposition", "mitochondira~1", lambda r: "fuzzy-mito" in r, _OK),
    ("fuzzy-two", "kubernates~2", lambda r: "fuzzy-kube" in r, _OK),
    # --- P4: competitive operators ---
    # Free via parse_query (locked as regression guards):
    ("required-prohibited", "+cross -loss", lambda r: "cross-only" in r and "all3" not in r, _OK),
    ("phrase-slop-alias", '"cross loss"~3', lambda r: "all3" in r and "stem-sing" not in r, _OK),
    (
        "negate-field-clause",
        "diffusion NOT kind:pdf",
        lambda r: "kind-docx" in r and "kind-pdf" not in r,
        _OK,
    ),
    # Implemented this phase:
    (
        "regex",
        "/crypto.*/",
        lambda r: {"wc-crypto", "wc-graphy", "wc-graphic"} <= r and "wc-other" not in r,
        _OK,
    ),
    ("wildcard-leading", "*graph", lambda r: "wc-graphic" in r and "wc-crypto" not in r, _OK),
    (
        "field-grouping",
        "title:(transformer OR nonexistentxyz)",
        lambda r: r == {"fld-title"},
        _OK,
    ),
    ("has-field", "has:author", lambda r: r == {"fld-author"}, _OK),
    # Field grouping composed with a boolean stays in content (filter extraction
    # leaves it) — it must reach Tantivy's parser, not the AST (which would crash
    # splitting ``title:`` from its group).
    (
        "field-group-in-boolean",
        "title:(transformer OR zzz) AND networks",
        lambda r: r == {"fld-title"},
        _OK,
    ),
    # A wildcard beside a field group must keep its wildcard semantics (the group
    # is parsed by Tantivy, the wildcard still compiles through the AST).
    (
        "field-group-plus-wildcard",
        "title:(transformer OR zzz) AND network*",
        lambda r: r == {"fld-title"},
        _OK,
    ),
    # Presence on a numeric field is "has a real (non-zero) value" — pages 5/15/25,
    # not the page:0 default of every other doc.
    ("has-uint-presence", "has:page", lambda r: r == {"pg-5", "pg-15", "pg-25"}, _OK),
    # --- Operators composing INSIDE booleans/parens (boolean AST compiler) ---
    # The discriminator in each: the dropped ``*``/no-op ``~N`` of the old
    # parse_query handoff would intersect on the *literal* stem and return {} —
    # only a real wildcard/fuzzy/regex leaf inside the AND yields the doc.
    (
        "and-wildcard",
        "crypto* AND messages",  # crypto*→cryptographi, AND messages → only wc-graphy
        lambda r: r == {"wc-graphy"},
        _OK,
    ),
    (
        "and-fuzzy",
        "kubernates~2 AND containers",  # ~2→kubernetes, AND containers → fuzzy-kube
        lambda r: r == {"fuzzy-kube"},
        _OK,
    ),
    (
        "and-regex",
        "/crypto.*/ AND hash",  # regex→cryptograph, AND hash → wc-graphic
        lambda r: r == {"wc-graphic"},
        _OK,
    ),
    (
        "group-wildcard-and",
        "(cryptozzz OR crypto*) AND messages",  # grouped wildcard branch → wc-graphy
        lambda r: r == {"wc-graphy"},
        _OK,
    ),
    (
        "wildcard-exclude",
        "crypto* -wallet",  # all crypto*, minus the wallet doc
        lambda r: {"wc-graphy", "wc-graphic"} <= r and "wc-crypto" not in r,
        _OK,
    ),
]


def _params() -> list[object]:
    out: list[object] = []
    for name, query, pred, broken in _CASES:
        for path in ("single", "layered"):
            marks = (
                (pytest.mark.xfail(strict=True, reason=f"{name}: unimplemented on {path} (P3)"),)
                if path in broken
                else ()
            )
            out.append(pytest.param(name, query, pred, path, marks=marks, id=f"{name}-{path}"))
    return out


@pytest.mark.parametrize(("name", "query", "pred", "path"), _params())
def test_query_capability(
    searcher: Searcher, name: str, query: str, pred: Callable[[set[str]], bool], path: str
) -> None:
    run = _single if path == "single" else _layered
    assert pred(run(searcher, query)), f"{name} [{path}] failed"


def test_weighted_default_ranking_layered(searcher: Searcher) -> None:
    """Weighted default (TUI/fusion path): bare multi-term retrieves OR but ranks
    all-term docs above single-term docs. This is the user's model and it holds
    on the fusion path today — the rework must preserve it once wildcard/fuzzy
    terms also resolve. (Plain BM25-over-OR does NOT guarantee this; fusion/RRF
    does — which is why single-pass CLI needs unifying, see below.)"""
    p = QueryPlan.from_user_text("cross entropy loss")
    groups = search_layered(searcher, query=p.lexical, limit=50, sections_per_file=5)
    ranked = [h.parent_id for g in groups for h in g.hits]
    assert "all3" in ranked
    all3_rank = ranked.index("all3")
    for single_term in ("cross-only", "entropy-only"):
        if single_term in ranked:
            assert all3_rank < ranked.index(single_term), (
                f"all-term doc must outrank {single_term}: {ranked}"
            )


def test_weighted_default_ranking_cli(searcher: Searcher) -> None:
    """Single-pass CLI `search` ranks all-term docs first like the TUI. Raw BM25
    over an OR doesn't guarantee this (a short doc matching one rarer term can
    outrank a doc matching every term); `search` now unifies on fusion (RRF)."""
    hits = searcher.search("cross entropy loss", limit=50)
    ranked = [h.parent_id for h in hits]
    all3_rank = ranked.index("all3")
    assert all3_rank < ranked.index("cross-only")


def test_unparsable_numeric_filter_does_not_crash() -> None:
    """An invalid numeric/date bound must not blow up extraction — the clause
    falls back to content (0 filters) instead of raising. Guards the `>N` /
    `[lo TO hi]` branches that previously let `ValueError` escape."""
    from fnd.query_filters import extract_filters

    schema = build_schema()
    for q in ("page:>abc", "mtime:[2024-13-01 TO 10]", "slide:[x TO y]"):
        assert extract_filters(q, schema).filters == []  # no exception, no filter
    assert len(extract_filters("page:>20", schema).filters) == 1  # valid still works
