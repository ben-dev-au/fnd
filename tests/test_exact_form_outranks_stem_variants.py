"""A chunk holding the word as typed outranks one holding only a stem variant of it."""

from __future__ import annotations

from pathlib import Path

import pytest

from fnd.index import build_index
from fnd.layered import search_layered
from fnd.query import Searcher

_PAD = "The quick brown fox jumps over the lazy dog near the river bank today. " * 3


def _searcher(
    tmp_path: Path, index_dir: Path, files: dict[str, str], titles: dict[str, str] | None = None
) -> Searcher:
    """One single-chunk file per entry, equal padding round each body."""
    root = tmp_path / "notes"
    root.mkdir()
    for name, body in files.items():
        title = (titles or {}).get(name, "Notes")
        (root / f"{name}.md").write_text(
            f"---\ntitle: {title}\n---\n# Notes\n\n{_PAD}{body}. {_PAD}\n"
        )
    build_index(roots=[root], index_dir=index_dir, collection="c")
    return Searcher(index_dir=index_dir)


def _rank(searcher: Searcher, query: str) -> list[str]:
    return [Path(g.path).stem for g in search_layered(searcher, query=query, limit=10)]


def _scores(searcher: Searcher, query: str) -> dict[str, float]:
    """Each file's raw score for ``query``."""
    pool = searcher._candidates(query, window=50, collection=None)  # pyright: ignore[reportPrivateUsage]
    return {Path(h.path).stem: h.score for h in pool.hits}


# Each variant file holds its form twice to the literal's once: stemming alone ranks them first.
_FILES = {
    "a_variant": "collections and collections",
    "b_literal": "collect()",
    "c_variant": "collected and collected",
}


@pytest.mark.parametrize("query", ["collect", "collect()", '"collect()"', "COLLECT"])
def test_the_word_as_typed_ranks_above_its_stem_variants(
    tmp_path: Path, tmp_index_dir: Path, query: str
) -> None:
    assert _rank(_searcher(tmp_path, tmp_index_dir, _FILES), query)[0] == "b_literal"


def test_a_typed_variant_ranks_above_the_stem_it_shares(
    tmp_path: Path, tmp_index_dir: Path
) -> None:
    files = {"a_typed": "collections", "c_variant": "collected and collected"}
    assert _rank(_searcher(tmp_path, tmp_index_dir, files), "collections")[0] == "a_typed"


def test_stem_variants_still_match(tmp_path: Path, tmp_index_dir: Path) -> None:
    assert set(_rank(_searcher(tmp_path, tmp_index_dir, _FILES), "collect")) == set(_FILES)


def test_a_pattern_beside_a_plain_word_keeps_its_whole_weight(
    tmp_path: Path, tmp_index_dir: Path
) -> None:
    s = _searcher(tmp_path, tmp_index_dir, {"w": "cryptography cryptography", "p": "invoice"})
    for pattern in ("crypt*", "cryptografy~"):
        assert _scores(s, f"{pattern} invoice")["w"] == pytest.approx(_scores(s, pattern)["w"])


def test_a_boost_scales_the_word_as_typed_too(tmp_path: Path, tmp_index_dir: Path) -> None:
    s = _searcher(tmp_path, tmp_index_dir, {"a": "collect", "b": "collections collections"})
    for name in ("a", "b"):
        assert _scores(s, "collect^3")[name] == pytest.approx(3 * _scores(s, "collect")[name])


def test_a_word_scoped_to_another_field_earns_no_body_credit(
    tmp_path: Path, tmp_index_dir: Path
) -> None:
    files = {"x": "baz", "y": "baz bar bar bar"}
    s = _searcher(tmp_path, tmp_index_dir, files, titles={"x": "foo bar"})
    assert _scores(s, 'title:"foo bar" OR baz')["y"] == pytest.approx(_scores(s, "baz")["y"])


def test_a_colon_that_names_no_field_reads_as_text(tmp_path: Path, tmp_index_dir: Path) -> None:
    s = _searcher(tmp_path, tmp_index_dir, {"d": "TODO: fix it", "e": "fix it later"})
    assert _scores(s, 'TODO:"fix it"') == pytest.approx(_scores(s, '"TODO fix it"'))


def test_a_negated_phrase_rewards_none_of_its_words(tmp_path: Path, tmp_index_dir: Path) -> None:
    """The kept chunk holds the phrase's words apart, so only the phrase is excluded."""
    s = _searcher(tmp_path, tmp_index_dir, {"g": "gather collect the data", "h": "gather"})
    assert _scores(s, 'gather NOT "collect data"') == pytest.approx(_scores(s, "gather"))


def test_a_phrase_ranks_by_the_phrase_not_its_words_apart(
    tmp_path: Path, tmp_index_dir: Path
) -> None:
    files = {"a": "follow up email", "b": "follow up email. " + "email " * 15}
    scores = _scores(_searcher(tmp_path, tmp_index_dir, files), '"follow up email"')
    assert scores["a"] > scores["b"]
