"""The model reorders and adds files by reciprocal rank, and never returns what fnd could not."""

from __future__ import annotations

import dataclasses
import math
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")

import fnd.semantic_search as semantic_search  # noqa: E402
from fnd.index import build_index  # noqa: E402
from fnd.layered import search_layered  # noqa: E402
from fnd.query import FileGroup, Searcher, decoded_body, file_key  # noqa: E402
from fnd.semantic import section_key, section_text  # noqa: E402
from fnd.semantic_search import SemanticContext, plain_content  # noqa: E402
from tests._semantic_fakes import FakeEmbedder, build_context, every_section  # noqa: E402

_NOTES = {
    "orchard.md": "# Orchard\n\n## Picking\n\napple apple harvest notes\n\n## Storage\n\napple crates in the shed\n",
    "harbour.md": "# Harbour\n\n## Moorings\n\nboat boat mooring fees and the apple stall\n",
    "weather.md": "# Weather\n\n## Today\n\ncloud cover over the bay, a boat on the water\n",
    "fruit.md": "# Fruit\n\n## List\n\npears, plums and one apple\n",
    "copy1.md": "# Copy\n\n## Same\n\nboat and apple, the same note twice\n",
    "copy2.md": "# Copy\n\n## Same\n\nboat and apple, the same note twice\n",
    "band.md": "# Band\n\n## Kit\n\ndrum drum drum and a flute\n",
    "orchestra.md": "# Orchestra\n\n## Solo\n\na piccolo solo and a drum on Sunday\n",
    "winds.txt": "a piccolo and an eagle over the water\n",
}
_BOOKS = {
    "sailing.md": "# Sailing\n\n## Rigging\n\nboat boat boat rigging and sails\n",
    "trees.md": "# Trees\n\n## Fruit trees\n\napple trees need pruning in winter\n",
    "music.md": "# Music\n\n## Wind\n\nflute flute and eagle songs\n",
    "birds.md": "# Birds\n\n## Calls\n\nflute flute flute like calls\n",
    "concert.md": "# Concert\n\n## Practice\n\npiccolo practice notes\n",
}
_FILLER = "A plain sentence about nothing in particular."


def _filler(i: int) -> str:
    """Distinct vectors, so no two fillers tie on the model side."""
    return f"# Filler {i}\n\n{_FILLER} {' '.join(['gourd'] * (i + 1))}\n"


def _write(root: Path, files: dict[str, str]) -> None:
    root.mkdir(exist_ok=True)
    for name, text in files.items():
        (root / name).write_text(text, encoding="utf-8")


def _build(
    tmp_path: Path, index_dir: Path, notes: dict[str, str], books: dict[str, str]
) -> Searcher:
    _write(tmp_path / "notes", notes | {f"filler{i}.md": _filler(i) for i in range(12)})
    _write(tmp_path / "books", books)
    build_index(roots=[tmp_path / "notes"], index_dir=index_dir, collection="notes")
    build_index(roots=[tmp_path / "books"], index_dir=index_dir, collection="books")
    return Searcher(index_dir=index_dir)


@pytest.fixture
def searcher(tmp_path: Path, tmp_index_dir: Path) -> Searcher:
    return _build(tmp_path, tmp_index_dir, _NOTES, _BOOKS)


@pytest.fixture
def ctx(searcher: Searcher, tmp_path: Path) -> SemanticContext:
    return build_context(searcher, tmp_path / "vectors")


def _search(
    searcher: Searcher,
    query: str,
    *,
    semantic: SemanticContext | None = None,
    limit: int = 10,
    collection: str | None = None,
    collapse: bool = True,
) -> list[FileGroup]:
    return search_layered(
        searcher,
        query=query,
        limit=limit,
        sections_per_file=3,
        collection=collection,
        collapse_copies=collapse,
        semantic=semantic,
    )


def _names(groups: list[FileGroup]) -> list[str]:
    return [Path(g.path).name for g in groups]


def _model_files(
    searcher: Searcher,
    ctx: SemanticContext,
    query: str,
    *,
    collection: str | None = None,
    collapse: bool = True,
) -> list[str]:
    """Files by best-section cosine within scope, worked out from the corpus and the fake."""
    qv = ctx.embedder.embed([query])[0]
    best: dict[str, float] = {}
    wanted = None if collection is None else {collection}
    for h in every_section(searcher):
        cols = {c for c, _ in h.memberships}
        if not cols & set(ctx.opted_in) or (wanted is not None and not cols & wanted):
            continue
        v = ctx.embedder.embed([section_text(h.title, h.heading_path, decoded_body(h)[0])])[0]
        k = file_key(h, collapse_copies=collapse)
        best[k] = max(best.get(k, -1.0), float(v @ qv))
    return sorted(best, key=lambda k: -best[k])


def _rrf(fnd_keys: list[str], model_keys: list[str], has_vectors: set[str]) -> list[str]:
    score: dict[str, float] = {}
    fpos = {k: i for i, k in enumerate(fnd_keys)}
    mpos = {k: i for i, k in enumerate(model_keys)}
    for k, i in fpos.items():
        score[k] = 1 / (61 + i)
        m = mpos.get(k, i if k not in has_vectors else None)
        if m is not None:
            score[k] += 1 / (61 + m)
    for k, i in mpos.items():
        if k not in fpos:
            score[k] = 1 / (61 + i)
    return sorted(score, key=lambda k: (-score[k], fpos.get(k, math.inf), mpos.get(k, math.inf)))


def _keys(groups: list[FileGroup]) -> list[str]:
    return [file_key(g.hits[0], collapse_copies=True) for g in groups]


def test_rank_fusion_reproduces_the_audited_arithmetic(
    searcher: Searcher, ctx: SemanticContext
) -> None:
    """Files order by 1/(61 + fnd position) + 1/(61 + model position)."""
    fnd = _keys(_search(searcher, "apple boat", limit=50))
    model = _model_files(searcher, ctx, "apple boat")
    expected = _rrf(fnd, model, has_vectors=set(model))[:10]
    assert _keys(_search(searcher, "apple boat", semantic=ctx)) == expected


def test_unembedded_files_keep_their_fnd_position(searcher: Searcher, tmp_path: Path) -> None:
    """A file without vectors takes its fnd position on the model side."""
    half = build_context(
        searcher, tmp_path / "half", only_files={"sailing.md", "weather.md", "band.md"}
    )
    fnd = _keys(_search(searcher, "apple boat", limit=50))
    pids = set(half.rows.parent_id.tolist())
    with_vectors = {
        file_key(h, collapse_copies=True) for h in every_section(searcher) if h.parent_id in pids
    }
    model = [k for k in _model_files(searcher, half, "apple boat") if k in with_vectors]
    expected = _rrf(fnd, model, has_vectors=with_vectors)[:10]
    assert _keys(_search(searcher, "apple boat", semantic=half)) == expected


@pytest.mark.parametrize(
    "query",
    [
        "kind:md",
        "the and",
        '"apple boat"',
        "{3} apple boat",
        "appl*",
        "apple -boat",
        "/app.e/",
        "apple~1",
        "apple AND boat",
    ],
)
def test_filter_only_and_stopword_queries_skip_the_model(
    searcher: Searcher, ctx: SemanticContext, query: str
) -> None:
    """Filters, stopwords and every operator leave fnd's result unchanged."""
    assert _names(_search(searcher, query, semantic=ctx)) == _names(_search(searcher, query))


@pytest.mark.parametrize(
    ("query", "content"),
    [
        ("C++ apple", "C++ apple"),
        ("TCP/IP", "TCP/IP"),
        ("kind:md apple", "apple"),
        ("apple OR boat", "apple boat"),
        ("(apple OR boat) flute", "apple boat flute"),
    ],
)
def test_plain_punctuated_words_still_use_the_model(
    searcher: Searcher, query: str, content: str
) -> None:
    """Punctuated words, inline field filters and OR groups still count as plain words;
    the model embeds the words alone."""
    assert plain_content(searcher, query) == content


def test_masking_happens_before_ranking(
    searcher: Searcher, ctx: SemanticContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A small collection's model candidates are its own sections, not a global top's survivors."""
    monkeypatch.setattr(semantic_search, "MODEL_SECTIONS", 2)
    names = _names(_search(searcher, "flute", semantic=ctx, collection="notes"))
    assert "orchestra.md" in names


def test_copies_count_once_on_the_model_side(searcher: Searcher, ctx: SemanticContext) -> None:
    """Two identical files give one result when copies collapse."""
    names = _names(_search(searcher, "boat apple", semantic=ctx, limit=20))
    assert len({"copy1.md", "copy2.md"} & set(names)) == 1


def test_opted_out_collections_take_no_part(searcher: Searcher, tmp_path: Path) -> None:
    """A collection opted out after it was embedded gives no model candidates."""
    both = build_context(searcher, tmp_path / "both")
    notes_only = dataclasses.replace(both, opted_in=frozenset({"notes"}))
    groups = _search(searcher, "flute", semantic=notes_only, limit=20)
    found = [g for g in groups if g.hits and all(h.found_by_meaning for h in g.hits)]
    assert "orchestra.md" in {Path(g.path).name for g in found}
    assert all(c == "notes" for g in found for c, _ in g.memberships)


def test_fusion_depth_does_not_follow_the_display_limit(
    searcher: Searcher, ctx: SemanticContext
) -> None:
    """limit=10 and limit=50 order their shared first files the same."""
    ten = _names(_search(searcher, "apple boat", semantic=ctx, limit=10))
    fifty = _names(_search(searcher, "apple boat", semantic=ctx, limit=50))
    assert fifty[: len(ten)] == ten


def test_model_found_hits_are_fnd_hits(searcher: Searcher, ctx: SemanticContext) -> None:
    """A model-only file's sections are indexed sections, marked found by meaning."""
    fnd_files = set(_names(_search(searcher, "eagle", limit=50)))
    groups = _search(searcher, "eagle", semantic=ctx, limit=20)
    extra = [g for g in groups if Path(g.path).name not in fnd_files]
    assert extra
    for g in extra:
        for h in g.hits:
            assert h.found_by_meaning
            again = searcher.sections(
                [(h.parent_id, h.chunk_seq)],
                collection=None,
                source_scope=None,
                metadata_filter=None,
                tag_filter=None,
            )
            assert [(x.parent_id, x.chunk_seq) for x in again] == [(h.parent_id, h.chunk_seq)]


def test_rows_keep_fnds_score(searcher: Searcher, ctx: SemanticContext) -> None:
    """Every file fnd found keeps its own score; a model-only file shows its similarity."""
    plain = {Path(g.path).name: g.top_score for g in _search(searcher, "apple", limit=50)}
    for g in _search(searcher, "apple", semantic=ctx, limit=20):
        name = Path(g.path).name
        if name in plain:
            assert g.top_score == plain[name]
            assert not any(h.found_by_meaning for h in g.hits)
        else:
            assert 0.0 < g.top_score <= 1.0


def test_lockstep_after_unreconciled_index_changes(tmp_path: Path, tmp_index_dir: Path) -> None:
    """After edits, deletions and a move with no re-embedding, every model hit is current."""
    searcher = _build(tmp_path, tmp_index_dir, _NOTES, _BOOKS)
    ctx = build_context(searcher, tmp_path / "vectors")
    (tmp_path / "notes" / "band.md").write_text(
        "# Band\n\n## Kit\n\ncloud cloud cloud\n", encoding="utf-8"
    )
    (tmp_path / "notes" / "weather.md").unlink()
    (tmp_path / "books" / "music.md").rename(tmp_path / "notes" / "music.md")
    build_index(roots=[tmp_path / "notes"], index_dir=tmp_index_dir, collection="notes")
    build_index(roots=[tmp_path / "books"], index_dir=tmp_index_dir, collection="books")
    searcher = Searcher(index_dir=tmp_index_dir)
    current = {(h.parent_id, h.chunk_seq): h for h in every_section(searcher)}
    stale = dict(
        zip(
            zip(ctx.rows.parent_id.tolist(), ctx.rows.chunk_seq.tolist(), strict=True),
            ctx.rows.text_key.tolist(),
            strict=True,
        )
    )
    embedder = FakeEmbedder()
    for query, collection in [
        ("drum flute", None),
        ("cloud boat", None),
        ("eagle", "books"),
        ("flute eagle", "notes"),
    ]:
        for g in _search(searcher, query, semantic=ctx, limit=20, collection=collection):
            for h in g.hits:
                if not h.found_by_meaning:
                    continue
                key = (h.parent_id, h.chunk_seq)
                assert key in current
                now = current[key]
                text = section_text(now.title, now.heading_path, decoded_body(now)[0])
                assert section_key(embedder.token_ids(text), 256) == stale[key]
                assert collection is None or collection in {c for c, _ in now.memberships}


def test_a_filters_pane_clause_reaches_the_model_side(
    searcher: Searcher, ctx: SemanticContext
) -> None:
    """A Filters-pane clause (FilteredSearcher) narrows the model's files as it narrows fnd's."""
    from fnd.query import FilteredSearcher

    filtered = FilteredSearcher(searcher, clauses=["kind:md"])
    groups = search_layered(filtered, query="flute", limit=20, semantic=ctx)  # type: ignore[arg-type]
    assert groups
    assert all(g.kind == "md" for g in groups)


@pytest.mark.parametrize(
    ("query", "keep"), [("kind:md flute", "kind"), ("c:books flute", "collection")]
)
def test_inline_filters_reach_the_model_side(
    searcher: Searcher, ctx: SemanticContext, query: str, keep: str
) -> None:
    """Filters typed in the query bind the model's files too."""
    groups = _search(searcher, query, semantic=ctx, limit=20)
    assert groups
    if keep == "kind":
        assert all(g.kind == "md" for g in groups)
    else:
        assert all(any(c == "books" for c, _ in g.memberships) for g in groups)


def test_a_stale_index_check_tokenises_nothing(tmp_path: Path, tmp_index_dir: Path) -> None:
    """After an index change, candidates are checked by their stored digest, not re-tokenised."""
    searcher = _build(tmp_path, tmp_index_dir, _NOTES, _BOOKS)
    embedder = FakeEmbedder()
    ctx = build_context(searcher, tmp_path / "vectors", embedder=embedder)
    (tmp_path / "notes" / "band.md").write_text(
        "# Band\n\n## Kit\n\ncloud cloud cloud\n", encoding="utf-8"
    )
    build_index(roots=[tmp_path / "notes"], index_dir=tmp_index_dir, collection="notes")
    searcher = Searcher(index_dir=tmp_index_dir)
    before = embedder.tokenised
    _search(searcher, "drum flute", semantic=ctx, limit=20)
    assert embedder.tokenised == before
