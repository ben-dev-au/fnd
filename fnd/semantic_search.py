"""fnd's file ranking blended with the meaning model's, by reciprocal rank.

The model only reorders and adds files fnd itself could return. Its rows are masked to the
search's collections and the opted-in ones before ranking; its candidates are re-read through
the same searcher snapshot, scope and filters as fnd's own hits; and when the index has moved
on since the vectors were filed, a candidate whose text no longer matches its vector is dropped.
A stale vector can therefore cost a result but never show a wrong one. Weights and depths are
the ones the owner audited (spec D7).
"""

from __future__ import annotations

import dataclasses
import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from fnd.explain import SemanticTrace
from fnd.query import FileGroup, Hit, decoded_body, file_key, group_by_file
from fnd.semantic import GRANITE, section_key, section_text

if TYPE_CHECKING:
    import numpy as np

    from fnd.config import Config
    from fnd.query import Searcher, SourceScope
    from fnd.semantic import Embedder
    from fnd.tag_query import TagFilter
    from fnd.vectors import RowTable, Snapshot

__all__ = [
    "LEXICAL_DEPTH",
    "SemanticContext",
    "blend",
    "context_for",
    "load_context",
    "plain_content",
    "skip_reason",
]

K: Final = 60
LEXICAL_DEPTH: Final = 50
MODEL_SECTIONS: Final = 2000
CLOSEST_SECTIONS: Final = 3
_BLOCK: Final = 16_384


@dataclass(frozen=True, slots=True)
class SemanticContext:
    embedder: Embedder
    snapshot: Snapshot
    rows: RowTable
    opted_in: frozenset[str]


QUERY_THREADS: Final = 2


def load_context(embedder: Embedder) -> SemanticContext | None:
    """The saved vectors and row table, or None before the first reconcile saved any."""
    from fnd.semantic import GRANITE
    from fnd.vectors import RowTable, VectorStore, store_dir, store_header

    folder = store_dir(GRANITE)
    if not (folder / "manifest.json").is_file():
        return None
    rows = RowTable.load(folder)
    if rows is None:
        return None
    store = VectorStore.open(folder, dim=embedder.dim, header=store_header(GRANITE))
    snapshot = store.snapshot()
    # After the snapshot: it re-reads the store if a writer merged or compacted meanwhile.
    stale = rows.store_generation != store.generation
    if stale or (len(rows.vec_row) and int(rows.vec_row.max()) >= len(snapshot.vecs)):
        return None
    return SemanticContext(
        embedder=embedder, snapshot=snapshot, rows=rows, opted_in=frozenset(rows.collections)
    )


def context_for(config: Config) -> SemanticContext | None:
    """A one-off context for a CLI search: None unless installed, on and embedded."""
    from fnd.semantic import is_ready, load_embedder

    if not config.defaults.semantic_search or not is_ready():
        return None
    ctx = load_context(load_embedder(threads=QUERY_THREADS))
    if ctx is None:
        return None
    opted = frozenset(n for n, c in config.collections.items() if c.semantic)
    return dataclasses.replace(ctx, opted_in=opted)


def plain_content(searcher: Searcher, query: str) -> str | None:
    """The words to embed: the content after inline filters, if it holds only plain words."""
    from fnd.query_ast import Or, Term, parse_query_ast, walk
    from fnd.query_dsl import preprocess
    from fnd.query_filters import extract_filters
    from fnd.schema import build_schema
    from fnd.stopwords import is_all_stopwords, strip_query_stopwords

    content = extract_filters(query, build_schema(), searcher._index).content.strip()
    if not content or is_all_stopwords(content):
        return None
    node = parse_query_ast(strip_query_stopwords(preprocess(content)))
    if node is None or not all(isinstance(n, Term | Or) for n in walk(node)):
        return None
    return content


def _scope_columns(
    rows: RowTable, collection: str | list[str] | None, opted_in: frozenset[str]
) -> list[int]:
    wanted = (
        set(rows.collections)
        if collection is None
        else {collection}
        if isinstance(collection, str)
        else set(collection)
    )
    return [i for i, c in enumerate(rows.collections) if c in wanted and c in opted_in]


def _admissible(ctx: SemanticContext, collection: str | list[str] | None) -> list[int]:
    import numpy as np

    cols = _scope_columns(ctx.rows, collection, ctx.opted_in)
    return np.flatnonzero(ctx.rows.member[:, cols].any(axis=1)).tolist() if cols else []


def skip_reason(
    searcher: Searcher, ctx: SemanticContext, query: str, collection: str | list[str] | None
) -> str | None:
    """Why this search runs on words alone, or None when the model takes part."""
    if plain_content(searcher, query) is None:
        return "not plain words"
    if not _admissible(ctx, collection):
        return "no vectors in scope"
    return None


def _unchanged(ctx: SemanticContext, row: int, hit: Hit) -> bool:
    """The section still holds the text its vector was made from: by stored digest,
    which needs no decoding; by key only for tables filed before digests existed."""
    from fnd.vectors import stored_digest

    rows = ctx.rows
    if rows.stored_digest is not None:
        return stored_digest(hit) == str(rows.stored_digest[row])
    text = section_text(hit.title, hit.heading_path, decoded_body(hit)[0])
    return section_key(ctx.embedder.token_ids(text), GRANITE.max_tokens) == str(rows.text_key[row])


def _ranked_rows(
    ctx: SemanticContext, admissible: list[int], query_vec: np.ndarray
) -> list[tuple[int, float]]:
    import numpy as np

    rows = ctx.rows
    idx = np.asarray(admissible, dtype=np.int64)
    vec_rows = rows.vec_row[idx]
    sims = np.empty(len(idx), np.float32)
    for start in range(0, len(idx), _BLOCK):
        part = ctx.snapshot.vecs[vec_rows[start : start + _BLOCK]].astype(np.float32)
        sims[start : start + _BLOCK] = part @ query_vec
    top = min(MODEL_SECTIONS, len(idx))
    pick = np.argpartition(-sims, top - 1)[:top] if top < len(idx) else np.arange(len(idx))
    pick = pick[np.argsort(-sims[pick], kind="stable")]
    return [(int(idx[i]), float(sims[i])) for i in pick]


def blend(
    groups: list[FileGroup],
    *,
    searcher: Searcher,
    ctx: SemanticContext,
    query: str,
    limit: int,
    sections_per_file: int,
    collection: str | list[str] | None,
    source_scope: SourceScope | None,
    metadata_filter: str | None,
    tag_filter: TagFilter | None,
    collapse_copies: bool,
    corrections: dict[str, tuple[str, ...]] | None = None,
) -> tuple[list[FileGroup], SemanticTrace]:
    """fnd's ``groups`` (ranked to at least :data:`LEXICAL_DEPTH`) blended with the model's, cut to ``limit``."""
    from fnd.typos import respelt

    content = plain_content(searcher, query)
    admissible = _admissible(ctx, collection)
    if content is None or not admissible:
        raise ValueError("blend called for a search skip_reason() keeps on words alone")
    rows = ctx.rows

    text = respelt(content.split(), corrections) if corrections else content
    ranked = _ranked_rows(ctx, admissible, ctx.embedder.embed([text])[0])
    keys = [(str(rows.parent_id[r]), int(rows.chunk_seq[r])) for r, _ in ranked]
    current = {
        (h.parent_id, h.chunk_seq): h
        for h in searcher.sections(
            keys,
            collection=collection,
            source_scope=source_scope,
            metadata_filter=metadata_filter,
            tag_filter=tag_filter,
            query=query,
        )
    }
    fresh = getattr(searcher, "generation", None) == rows.stamp

    model_order: list[str] = []
    model_hits: dict[str, list[Hit]] = {}
    for (row, sim), key in zip(ranked, keys, strict=True):
        hit = current.get(key)
        if hit is None:
            continue
        if not fresh and not _unchanged(ctx, row, hit):
            continue
        fk = file_key(hit, collapse_copies=collapse_copies)
        if fk not in model_hits:
            model_order.append(fk)
            model_hits[fk] = []
        if len(model_hits[fk]) < CLOSEST_SECTIONS:
            model_hits[fk].append(dataclasses.replace(hit, score=sim, found_by_meaning=True))

    fnd_keys = [
        file_key(g.hits[0], collapse_copies=collapse_copies) if g.hits else g.parent_id
        for g in groups
    ]
    fnd_pos = {k: i for i, k in enumerate(fnd_keys)}
    model_pos = {k: i for i, k in enumerate(model_order)}
    embedded = {str(p) for p in rows.parent_id[admissible].tolist()}
    score: dict[str, float] = {}
    has_vectors = {k for k, g in zip(fnd_keys, groups, strict=True) if g.parent_id in embedded}
    for k, i in fnd_pos.items():
        m = model_pos.get(k, None if k in has_vectors else i)
        score[k] = 1 / (K + 1 + i) + (1 / (K + 1 + m) if m is not None else 0.0)
    for k, i in model_pos.items():
        if k not in fnd_pos:
            score[k] = 1 / (K + 1 + i)
    order = sorted(
        score, key=lambda k: (-score[k], fnd_pos.get(k, math.inf), model_pos.get(k, math.inf))
    )

    by_key = dict(zip(fnd_keys, groups, strict=True))
    out: list[FileGroup] = []
    for k in order[:limit]:
        group = by_key.get(k)
        if group is None:
            (group,) = group_by_file(
                model_hits[k],
                limit=1,
                sections_per_file=sections_per_file,
                collapse_copies=collapse_copies,
            )
        out.append(group)
    ranks = {k: (fnd_pos.get(k), model_pos.get(k)) for k in order[:limit]}
    return out, SemanticTrace(applied=True, reason="blended", ranks=ranks)
