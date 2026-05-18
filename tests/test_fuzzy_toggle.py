"""Fuzzy toggle + min-chars + explicit ~N opt-in.

Pins the user-configurable behavior added in spec
``2026-05-19-fuzzy-search-config``:

* ``defaults.fuzzy_enabled = False`` disables the auto-fuzzy cascade
  pass entirely.
* Even with auto-fuzzy off, a per-term ``~N`` modifier still runs the
  cascade fuzzy pass for that term at distance N.
* ``defaults.fuzzy_min_term_chars`` raises the floor below which the
  AUTO heuristic returns distance 0.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fnd.cascade import _terms_with_fuzzy, cascade_search
from fnd.index import build_index
from fnd.query import Searcher


@pytest.fixture
def tpl_corpus(tmp_path: Path, tmp_index_dir: Path) -> Path:
    """A single MD file containing 'templates' (en_stem → ``templat``)."""
    root = tmp_path / "docs"
    root.mkdir(parents=True)
    (root / "tpl.md").write_text(
        "# Templates\nThe templates section starts here.\n",
        encoding="utf-8",
    )
    build_index(roots=[tmp_path], index_dir=tmp_index_dir, collection="default")
    return tmp_index_dir


# ── Parser ─────────────────────────────────────────────────────────


def test_terms_with_fuzzy_strips_unmodified_terms() -> None:
    assert _terms_with_fuzzy("hello world") == [("hello", None), ("world", None)]


def test_terms_with_fuzzy_extracts_per_term_distance() -> None:
    assert _terms_with_fuzzy("templat~1 foo") == [("templat", 1), ("foo", None)]
    assert _terms_with_fuzzy("templat~2") == [("templat", 2)]


def test_terms_with_fuzzy_clamps_distance_to_two() -> None:
    assert _terms_with_fuzzy("term~5") == [("term", 2)]


def test_terms_with_fuzzy_ignores_bare_tilde() -> None:
    # A bare ``~`` with no digit is not a fuzzy opt-in; the ``~`` is
    # stripped and the term reads as exact.
    assert _terms_with_fuzzy("term~") == [("term", None)]


def test_terms_with_fuzzy_ignores_phrase_proximity() -> None:
    # "a b"~3 is a proximity query, not a fuzzy modifier. Quoted
    # phrases are stripped before per-token parsing.
    assert _terms_with_fuzzy('"a b"~3 cat') == [("cat", None)]


# ── Cascade with toggle off ─────────────────────────────────────────


def test_cascade_fuzzy_off_returns_no_fuzzy_hits(tpl_corpus: Path) -> None:
    """With auto-fuzzy disabled, a misspelled query that *only* surfaces
    via the fuzzy pass returns no hits (literal pass finds nothing,
    synonym pass nothing without a synonym table)."""
    s = Searcher(index_dir=tpl_corpus)
    hits = cascade_search(
        s,
        query="templatas",
        threshold=1,
        auto_fuzzy_enabled=False,
    )
    assert hits == []


def test_cascade_fuzzy_off_but_explicit_tilde_still_matches(tpl_corpus: Path) -> None:
    """User opts in per-term with ``~1`` even when auto-fuzzy is off."""
    s = Searcher(index_dir=tpl_corpus)
    hits = cascade_search(
        s,
        query="templatas~1",
        threshold=1,
        auto_fuzzy_enabled=False,
    )
    paths = [Path(h.path).name for h in hits]
    assert "tpl.md" in paths


def test_cascade_min_term_chars_floor_blocks_short_stems(tpl_corpus: Path) -> None:
    """``min_term_chars=10`` puts the 8-char ``templatas`` stem below
    the floor → no fuzzy expansion even with auto-fuzzy on."""
    s = Searcher(index_dir=tpl_corpus)
    hits = cascade_search(
        s,
        query="templatas",
        threshold=1,
        auto_fuzzy_enabled=True,
        min_term_chars=10,
    )
    assert hits == []


def test_cascade_default_behavior_unchanged(tpl_corpus: Path) -> None:
    """Sanity: default params preserve existing fuzzy behavior."""
    s = Searcher(index_dir=tpl_corpus)
    hits = cascade_search(s, query="templatas", threshold=1)
    paths = [Path(h.path).name for h in hits]
    assert "tpl.md" in paths


# ── Highlighter ────────────────────────────────────────────────────


def test_matchspec_auto_fuzzy_off_drops_fuzzy_variants() -> None:
    from fnd.matching import MatchSpec, word_matches

    spec = MatchSpec.from_query("templatas", auto_fuzzy=False)
    # Exact stem still matches.
    assert word_matches("templatas", spec)
    # AUTO-distance variant no longer matches.
    assert not word_matches("templates", spec)


def test_matchspec_min_term_chars_suppresses_short_stems() -> None:
    from fnd.matching import MatchSpec, word_matches

    # Stem of "abcd" is "abcd" (len 4). Floor of 10 → no fuzzy.
    spec = MatchSpec.from_query("abcd", auto_fuzzy=True, min_term_chars=10)
    assert word_matches("abcd", spec)
    assert not word_matches("abce", spec)


def test_matchspec_explicit_tilde_paints_even_when_auto_off() -> None:
    from fnd.matching import MatchSpec, word_matches

    spec = MatchSpec.from_query("templatas~1", auto_fuzzy=False)
    # Explicit opt-in keeps fuzzy variants alive.
    assert word_matches("templates", spec)
