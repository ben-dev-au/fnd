"""Phase 8: cascading multi-pass + synonym expansion (§9c, §9e).

Two features land here:

* :func:`fnd.synonyms.expand` rewrites a query string by wrapping any
  single-term that matches a synonym group into ``(term OR sym1 OR sym2)``.
  Synonyms live in a user-owned TOML file (§6) and apply at *query time*
  only — the index never sees the expansion, so synonym edits don't require
  a rebuild.
* :func:`fnd.cascade.cascade_search` orchestrates a sequence of widening
  query passes. Pass 1 is the literal query; if the result count is below
  the per-pass threshold, pass 2 widens with fuzzy~1; pass 3 then expands
  via synonyms. Hits from later passes are *appended* (deduplicated) so the
  user always sees the more relevant exact matches first.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from fnd.cascade import cascade_search
from fnd.index import build_index
from fnd.query import Hit, Searcher
from fnd.synonyms import SynonymTable, expand, load_synonyms

# ── synonyms ───────────────────────────────────────────────────────


def test_synonyms_expand_wraps_known_term() -> None:
    table = SynonymTable.from_groups([["MSSM", "minimal supersymmetric standard model"]])
    out = expand("foo MSSM bar", table)
    # Multi-word synonyms must come back quoted so Tantivy parses them as a
    # phrase, not three separate OR'd terms.
    assert "MSSM" in out
    assert '"minimal supersymmetric standard model"' in out
    # Surrounding terms are untouched.
    assert "foo" in out
    assert "bar" in out


def test_synonyms_expand_is_bidirectional() -> None:
    """Either form of a synonym group expands to all members."""
    table = SynonymTable.from_groups([["MSSM", "minimal supersymmetric standard model"]])
    a = expand("MSSM", table)
    b = expand('"minimal supersymmetric standard model"', table)
    # Both contain MSSM and the long form.
    for q in (a, b):
        assert "MSSM" in q
        assert "minimal supersymmetric standard model" in q


def test_synonyms_unknown_term_passes_through() -> None:
    table = SynonymTable.from_groups([["MSSM", "minimal supersymmetric standard model"]])
    assert expand("unrelated phrase", table) == "unrelated phrase"


def test_synonyms_does_not_expand_inside_phrase() -> None:
    """A term inside a quoted phrase must not be expanded — the user asked
    for an exact phrase, so we respect that."""
    table = SynonymTable.from_groups([["MSSM", "minimal supersymmetric standard model"]])
    out = expand('"the MSSM model"', table)
    assert out == '"the MSSM model"'


def test_synonyms_load_from_toml(tmp_path: Path) -> None:
    p = tmp_path / "synonyms.toml"
    p.write_text(
        textwrap.dedent("""\
            [synonyms.science]
            groups = [
                ["MSSM", "minimal supersymmetric standard model"],
                ["chiral perturbation theory", "ChPT"],
            ]

            [synonyms.ops]
            groups = [["k8s", "kubernetes"]]
        """),
        encoding="utf-8",
    )
    table = load_synonyms(p)
    out = expand("k8s pod", table)
    assert "kubernetes" in out
    out = expand("ChPT calculation", table)
    assert "chiral perturbation theory" in out


def test_synonyms_load_missing_file_returns_empty(tmp_path: Path) -> None:
    table = load_synonyms(tmp_path / "does-not-exist.toml")
    # Empty table → no expansion, no error.
    assert expand("anything", table) == "anything"


# ── cascade ────────────────────────────────────────────────────────


@pytest.fixture
def small_md_corpus(tmp_path: Path, tmp_index_dir: Path) -> Path:
    """Three MD files: a/b share 'penguin sandwich' for exact tests; c only
    has 'penguin' (no 'sandwich'), so a query for the misspelled 'penquin'
    must fall through to the fuzzy pass to surface c."""
    root = tmp_path / "docs"
    root.mkdir(parents=True)
    (root / "a.md").write_text("# A\nthe blue penguin sandwich here.\n", encoding="utf-8")
    (root / "b.md").write_text("# B\nanother blue penguin sandwich.\n", encoding="utf-8")
    (root / "c.md").write_text("# C\nthe penguin lives here.\n", encoding="utf-8")
    build_index(roots=[tmp_path], index_dir=tmp_index_dir, collection="default")
    return tmp_index_dir


def test_cascade_returns_exact_pass_when_threshold_met(small_md_corpus: Path) -> None:
    """When the literal pass already returns enough hits, widening passes
    must not run — every hit must be tagged ``pass_index=0``."""
    s = Searcher(index_dir=small_md_corpus)
    hits = cascade_search(s, query="penguin sandwich", threshold=2)
    assert hits, "expected literal-pass hits"
    assert all(h.pass_index == 0 for h in hits)


def test_cascade_falls_back_to_fuzzy_on_misspelling(small_md_corpus: Path) -> None:
    """A query that exact-matches nothing must still find docs via fuzzy~1."""
    s = Searcher(index_dir=small_md_corpus)
    hits = cascade_search(s, query="penquin", threshold=1)
    paths = [Path(h.path).name for h in hits]
    # The misspelled c.md should be reachable via fuzzy.
    assert "c.md" in paths
    # The hit must be tagged as a fuzzy-pass result (pass_index >= 1).
    fuzzy = [h for h in hits if Path(h.path).name == "c.md"]
    assert fuzzy
    assert all(h.pass_index >= 1 for h in fuzzy)


def test_cascade_fuzzy_matches_when_indexed_token_is_stemmed(
    tmp_path: Path, tmp_index_dir: Path
) -> None:
    """Regression: ``F_BODY`` is indexed with ``en_stem``, so the on-disk
    token for "Templates" is ``templat``. A typo query like "Templatas"
    lowercases to ``templatas`` (8 chars). Without stemming the query,
    Tantivy's ``fuzzy_term_query`` sees distance 2 from ``templat`` and
    rejects the match — even though both forms come from the same root.
    Stem the query before issuing the fuzzy search so the on-disk
    Levenshtein distance is computed between the same token shapes the
    analyzer wrote.

    Picks word pairs the Snowball English stemmer actually reshapes
    (unlike the ``glimer/glimmer`` and ``penquin/penguin`` pairs the
    other tests use, where the stemmer leaves both forms untouched).
    """
    import snowballstemmer

    stemmer = snowballstemmer.stemmer("english")
    # Sanity-pin the test on stemmer behaviour: if the stemmer ever stops
    # reshaping these inputs the test stops exercising the bug.
    assert stemmer.stemWord("templates") == "templat"
    assert stemmer.stemWord("templatas") == "templata"

    root = tmp_path / "docs"
    root.mkdir(parents=True)
    (root / "tpl.md").write_text(
        "# Templates\nThe templates section starts here.\n",
        encoding="utf-8",
    )
    build_index(roots=[tmp_path], index_dir=tmp_index_dir, collection="default")

    s = Searcher(index_dir=tmp_index_dir)
    hits = cascade_search(s, query="templatas", threshold=1)
    paths = [Path(h.path).name for h in hits]
    assert "tpl.md" in paths, hits
    fuzzy = [h for h in hits if Path(h.path).name == "tpl.md"]
    assert fuzzy
    assert all(h.pass_index >= 1 for h in fuzzy)


def test_cascade_synonym_pass_finds_long_form(tmp_path: Path, tmp_index_dir: Path) -> None:
    """A query for 'MSSM' returns 0 exact hits, but cascading via the
    synonym pass must surface the document that uses the long form."""
    root = tmp_path / "papers"
    root.mkdir(parents=True)
    (root / "long.md").write_text(
        "# Notes\nThe minimal supersymmetric standard model has 105 parameters.\n",
        encoding="utf-8",
    )
    build_index(roots=[tmp_path], index_dir=tmp_index_dir, collection="default")

    s = Searcher(index_dir=tmp_index_dir)
    table = SynonymTable.from_groups([["MSSM", "minimal supersymmetric standard model"]])
    hits = cascade_search(s, query="MSSM", threshold=1, synonyms=table)
    paths = [Path(h.path).name for h in hits]
    assert "long.md" in paths
    syn_hits = [h for h in hits if Path(h.path).name == "long.md"]
    # Synonym pass is the last one (index 2 in the default chain).
    assert syn_hits
    assert all(h.pass_index >= 2 for h in syn_hits)


def test_cascade_does_not_duplicate_hits_across_passes(small_md_corpus: Path) -> None:
    """A document found in pass 1 must not also appear as a fuzzy pass-2
    duplicate. Dedup is by (parent_id, chunk_seq)."""
    s = Searcher(index_dir=small_md_corpus)
    hits = cascade_search(s, query="penguin", threshold=99)
    # Force a deep cascade: threshold=99 ensures all passes run.
    seen: set[tuple[str, int]] = set()
    for h in hits:
        key = (h.parent_id, h.chunk_seq)
        assert key not in seen, f"duplicate hit for chunk {key} across passes"
        seen.add(key)


def test_cascade_zero_hits_when_no_match(small_md_corpus: Path) -> None:
    s = Searcher(index_dir=small_md_corpus)
    hits = cascade_search(s, query="zzzunmatchablezzz", threshold=1)
    assert hits == []


def test_hit_pass_index_default_is_zero() -> None:
    """Existing Hit construction sites must keep working: pass_index defaults to 0."""
    h = Hit(
        score=1.0,
        parent_id="x",
        path="/x",
        kind="md",
        page=0,
        slide=0,
        heading_path="",
        title="",
        snippet="",
    )
    assert h.pass_index == 0


def test_format_hit_label_shows_per_pass_glyph() -> None:
    """The TUI tree label adds a glyph for fuzzy / synonym hits but stays
    quiet for the exact pass (the common case)."""
    from fnd.tui.results_labels import _format_hit_label

    def _make(pass_index: int) -> Hit:
        return Hit(
            score=1.23,
            parent_id="x",
            path="/x",
            kind="md",
            page=7,
            slide=0,
            heading_path="",
            title="",
            snippet="",
            pass_index=pass_index,
        )

    exact_label = _format_hit_label(_make(0))
    assert "~" not in exact_label
    assert "⊕" not in exact_label
    assert "~" in _format_hit_label(_make(1))
    assert "⊕" in _format_hit_label(_make(2))
