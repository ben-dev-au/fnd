"""Wildcards and regexes match the word as written, not its stem, and every
result they find shows its match."""

from __future__ import annotations

from pathlib import Path

import pytest

from fnd.index import build_index
from fnd.layered import search_layered
from fnd.matching import MatchSpec
from fnd.query import Searcher
from fnd.render import match_word_spans, text_has_any_match


@pytest.fixture
def searcher(tmp_path: Path, tmp_index_dir: Path) -> Searcher:
    root = tmp_path / "notes"
    root.mkdir()
    (root / "crypto.md").write_text("# Notes\n\nCryptography keeps public keys safe.\n")
    (root / "hash.md").write_text("# Notes\n\nA cryptographic hash.\n")
    (root / "data.md").write_text("# Notes\n\nNormalization of the records.\n")
    build_index(roots=[root], index_dir=tmp_index_dir, collection="c")
    return Searcher(index_dir=tmp_index_dir)


def _found(searcher: Searcher, query: str) -> set[str]:
    groups, trace = search_layered(searcher, query=query, limit=10, with_trace=True)
    spec = trace.paint_spec(
        MatchSpec.from_query(query), MatchSpec.from_query(query, auto_fuzzy=False)
    )
    unpainted = [g.path for g in groups if not text_has_any_match(Path(g.path).read_text(), spec)]
    assert not unpainted, f"{query}: results without a highlight: {unpainted}"
    return {Path(g.path).name for g in groups}


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("crypt*aphy", {"crypto.md"}),
        ("/cryptograph(y|ic)/", {"crypto.md", "hash.md"}),
        ("*ization", {"data.md"}),
        ('"crypt*aphy keeps"', {"crypto.md"}),
        ('"crypto* keeps"', {"crypto.md"}),
        ("{3}crypt*aphy keys", {"crypto.md"}),
        ("kryptography~1", {"crypto.md"}),
        ('"cryptographic~ keeps"', {"crypto.md"}),
        ("{3}kryptography~1 keys", {"crypto.md"}),
        ("{2}cryptography~ hash", {"hash.md"}),
        ("cryptographic~ NEAR/3 keys", {"crypto.md"}),
    ],
)
def test_a_pattern_finds_and_paints_the_written_word(
    searcher: Searcher, query: str, expected: set[str]
) -> None:
    assert _found(searcher, query) == expected


@pytest.fixture
def runners(tmp_path: Path, tmp_index_dir: Path) -> Searcher:
    root = tmp_path / "notes"
    root.mkdir()
    (root / "running.md").write_text("# Notes\n\nRunning is fun.\n")
    (root / "runs.md").write_text("# Notes\n\nShe runs fast.\n")
    build_index(roots=[root], index_dir=tmp_index_dir, collection="c")
    return Searcher(index_dir=tmp_index_dir)


@pytest.mark.parametrize("query", ['"runn* fast"', "{2}runn* fast"])
def test_a_wildcard_in_a_phrase_paints_the_word_its_stem_matched(
    runners: Searcher, query: str
) -> None:
    """Phrases match by position on stems: ``runn*`` reaches "running", whose stem
    "run" is also that of "runs"."""
    groups, trace = search_layered(runners, query=query, limit=10, with_trace=True)
    spec = trace.paint_spec(
        MatchSpec.from_query(query), MatchSpec.from_query(query, auto_fuzzy=False)
    )
    texts = {Path(g.path).name: Path(g.path).read_text() for g in groups}
    assert "runs.md" in texts
    assert all(text_has_any_match(t, spec) for t in texts.values())
    painted = {
        texts["runs.md"][a:b].lower() for a, b, _ in match_word_spans(texts["runs.md"], spec)
    }
    assert '"' in query or "runs" in painted, painted


@pytest.mark.asyncio
async def test_the_results_row_sees_a_phrase_wildcard_match(
    runners: Searcher, tmp_index_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The row's evidence check resolves phrase wildcards as the preview paint does."""
    import textwrap

    from fnd.config import load
    from fnd.tui import FNDApp
    from fnd.tui.match_evidence import has_paintable_match
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
        await run_search(pilot, app, '"runn* fast"')
        hits = [h for g in app._search.groups for h in g.hits]
        assert [Path(h.path).name for h in hits] == ["runs.md"]
        assert has_paintable_match(hits[0], app._search.evidence_spec)
