"""Highlights show what the search matched: fuzzy near-misses only where the
results came from the fuzzy pass, and never a result without a highlight."""

from __future__ import annotations

from pathlib import Path

import pytest

from fnd.index import build_index
from fnd.layered import search_layered
from fnd.matching import MatchSpec, word_matches
from fnd.query import Searcher
from fnd.render import text_has_any_match


@pytest.fixture
def searcher(tmp_path: Path, tmp_index_dir: Path) -> Searcher:
    root = tmp_path / "notes"
    root.mkdir()
    (root / "table.md").write_text("# Hashing\n\na hash table has buckets.\n")
    (root / "light.md").write_text("# Light\n\nthe glimmer of a lamp.\n")
    (root / "typo.md").write_text("# Typo\n\nsomeone wrote glimer here.\n")
    build_index(roots=[root], index_dir=tmp_index_dir, collection="c")
    return Searcher(index_dir=tmp_index_dir)


def _paint(searcher: Searcher, query: str) -> tuple[list[str], MatchSpec, list[str]]:
    groups, trace = search_layered(searcher, query=query, limit=10, with_trace=True)
    spec = trace.paint_spec(
        MatchSpec.from_query(query), MatchSpec.from_query(query, auto_fuzzy=False)
    )
    bodies = [Path(g.path).read_text() for g in groups]
    return [Path(g.path).name for g in groups], spec, bodies


def test_an_exact_match_paints_no_near_misses(searcher: Searcher) -> None:
    names, spec, _ = _paint(searcher, "hash table")
    assert names == ["table.md"]
    assert not word_matches("has", spec), "the search matched hash exactly; has is no match"
    assert word_matches("hash", spec)


def test_a_fuzzy_result_still_shows_its_match(searcher: Searcher) -> None:
    """``glimer`` is in the index, so it is not respelt; the fuzzy pass finds glimmer."""
    names, spec, bodies = _paint(searcher, "glimer")
    assert set(names) == {"light.md", "typo.md"}
    assert all(text_has_any_match(body, spec) for body in bodies), "every result is highlighted"


@pytest.mark.asyncio
async def test_the_tui_paints_what_it_searched(
    searcher: Searcher, tmp_index_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import textwrap

    from fnd.config import load
    from fnd.tui import FNDApp
    from tests._pilot_wait import run_search

    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        textwrap.dedent(f"""
            [[collections.c.sources]]
            path = "{(tmp_path / "notes").as_posix()}"
        """)
    )
    monkeypatch.setattr("fnd.config.default_config_path", lambda: cfg_path)
    app = FNDApp(index_dir=tmp_index_dir, config=load(cfg_path))
    async with app.run_test() as pilot:
        await run_search(pilot, app, "hash table")
        assert app._search.match_spec.fuzzy_per_stem == ()
        await run_search(pilot, app, "glimer")
        assert app._search.match_spec.fuzzy_per_stem != ()


@pytest.mark.parametrize(("query", "word"), [("glimer~", "glimmer"), ("2026~", "2025")])
def test_a_bare_tilde_paints_what_it_fuzzed(
    tmp_path: Path, tmp_index_dir: Path, query: str, word: str
) -> None:
    """A typed ``~`` with no distance is explicit fuzzy at the automatic distance."""
    root = tmp_path / "docs"
    root.mkdir()
    (root / "a.md").write_text(f"# A\n\nthe {word} here.\n")
    build_index(roots=[root], index_dir=tmp_index_dir, collection="c")
    groups, trace = search_layered(
        Searcher(index_dir=tmp_index_dir), query=query, limit=10, with_trace=True
    )
    spec = trace.paint_spec(
        MatchSpec.from_query(query), MatchSpec.from_query(query, auto_fuzzy=False)
    )
    assert [Path(g.path).name for g in groups] == ["a.md"]
    assert word_matches(word, spec)
