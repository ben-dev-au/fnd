"""A typed ``OR`` group scores as its best alternative, never the sum of them, so
holding both alternatives does not outrank holding one beside the query's other
words. The group also works inside ``{N}`` and ``NEAR/N``."""

from __future__ import annotations

from pathlib import Path

import pytest

from fnd.index import build_index
from fnd.layered import search_layered
from fnd.matching import MatchSpec
from fnd.query import FileGroup, Searcher
from fnd.query_errors import QuerySyntaxError
from fnd.query_plan import QueryPlan

_PAD = "The quick brown fox jumps over the lazy dog near the river bank today. " * 3
# Each file mentions each of its words once, padded to one length, under a neutral
# name and title: a query word in the path or title would score there too.
_FILES = {
    "both": "maven mvn",
    "maven_commands": "maven commands",
    "mvn_commands": "mvn commands",
    "all_three": "maven mvn commands",
    "maven": "maven",
    "mvn": "mvn",
    "commands": "commands",
}
_SECTIONS = {
    "One": "maven mvn",
    "Two": "mvn commands",
    "Three": "maven commands",
    "Four": "maven mvn commands",
}


def _body(words: str) -> str:
    return f"{_PAD}{words}. {_PAD}\n"


@pytest.fixture
def searcher(tmp_path: Path, tmp_index_dir: Path) -> Searcher:
    root = tmp_path / "notes"
    root.mkdir()
    for i, words in enumerate(_FILES.values()):
        (root / f"note{i}.md").write_text(f"# Note\n\n{_body(words)}")
    for i in range(12):  # common enough that "commands" weighs less than either alternative
        (root / f"filler{i}.md").write_text(
            f"# Filler\n\n{_body('commands' if i % 3 else 'gradle')}"
        )
    (root / "far.md").write_text(f"# Far\n\nmaven {_PAD * 4} commands\n")
    (root / "sections.md").write_text(
        "# Build notes\n\n" + "".join(f"## {h}\n\n{_body(w)}\n" for h, w in _SECTIONS.items())
    )
    build_index(roots=[root], index_dir=tmp_index_dir, collection="c")
    return Searcher(index_dir=tmp_index_dir)


def _search(searcher: Searcher, typed: str) -> list[FileGroup]:
    plan = QueryPlan.from_user_text(typed)
    return search_layered(searcher, query=plan.lexical, limit=50, sections_per_file=10)


def _label(path: str) -> str:
    stem = Path(path).stem
    return list(_FILES)[int(stem[4:])] if stem.startswith("note") else stem


def _files(searcher: Searcher, typed: str) -> list[str]:
    return [_label(g.path) for g in _search(searcher, typed)]


def _best_score(searcher: Searcher, typed: str, name: str) -> float:
    return next(g.hits[0].score for g in _search(searcher, typed) if _label(g.path) == name)


@pytest.mark.parametrize(
    "typed", ["maven OR mvn commands", "(maven OR mvn) commands", "maven|mvn commands"]
)
def test_either_alternative_beside_the_other_word_outranks_both_alternatives_alone(
    searcher: Searcher, typed: str
) -> None:
    """On summed scores "maven mvn" (2 rare words) beat "mvn commands" (1 rare, 1 common)."""
    order = _files(searcher, typed)
    assert order.index("mvn_commands") < order.index("both")
    assert order.index("maven_commands") < order.index("both")


def test_holding_both_alternatives_scores_no_higher_than_holding_one(searcher: Searcher) -> None:
    typed = "maven OR mvn commands"
    assert _best_score(searcher, typed, "all_three") <= _best_score(searcher, typed, "mvn_commands")


def test_a_files_sections_order_by_alternatives_too(searcher: Searcher) -> None:
    """A section's share of the query's words counts the OR group as one word."""
    group = next(
        g for g in _search(searcher, "maven OR mvn commands") if Path(g.path).stem == "sections"
    )
    score = {h.heading_path.rsplit(" > ", 1)[-1]: h.section_score or 0.0 for h in group.hits}
    assert score["Four"] <= score["Two"]
    assert score["One"] < score["Three"] < score["Two"]


def test_spaced_words_still_add_up(searcher: Searcher) -> None:
    order = _files(searcher, "maven mvn commands")
    assert order.index("both") < order.index("mvn_commands")


def test_fnds_own_or_rewrites_still_add_up(searcher: Searcher) -> None:
    """Synonym and respelling passes rank a section holding several forms higher."""
    pool = searcher._candidates("maven OR mvn", window=50, collection=None, alternatives=False)
    scores = {_label(h.path): h.score for h in pool.hits}
    assert scores["both"] > scores["mvn"]


def test_the_synonym_pass_still_adds_up(searcher: Searcher) -> None:
    """Measured: as alternatives, synonym queries lost 0.194 nDCG@10."""
    from fnd.fusion import fusion_search
    from fnd.synonyms import SynonymTable

    _, trace = fusion_search(
        searcher,
        query="maven",
        synonyms=SynonymTable.from_groups([["maven", "mvn"]]),
        with_trace=True,
    )
    by_file = {
        _label(h.path): h.parent_id
        for h in searcher._candidates("maven OR mvn", window=50, collection=None).hits
    }
    syn = {c.parent_id: c.bm25_per_source.get("syn", 0.0) for c in trace.contributions}
    assert syn[by_file["both"]] > syn[by_file["mvn"]]


_NEAR_FILES = {"maven_commands", "mvn_commands", "all_three", "sections"}


@pytest.mark.parametrize(
    "typed",
    [
        "{5} (maven OR mvn) commands",
        "{5} maven|mvn commands",
        "{5} commands (maven OR mvn)",
        "(maven OR mvn) NEAR/5 commands",
        "maven|mvn NEAR/5 commands",
    ],
)
def test_an_or_group_inside_nearness_finds_either_alternative_near(
    searcher: Searcher, typed: str
) -> None:
    """``far`` holds maven and commands a paragraph apart."""
    assert set(_files(searcher, typed)) == _NEAR_FILES


def test_an_or_group_inside_nearness_highlights_each_alternative(searcher: Searcher) -> None:
    spec = MatchSpec.from_query(QueryPlan.from_user_text("{5} (maven OR mvn) commands").lexical)
    assert {members for members, _slop in spec.proximity_groups} == {
        ("maven", "command"),
        ("mvn", "command"),
    }


def test_nearness_alternatives_score_as_alternatives(searcher: Searcher) -> None:
    typed = "{5} (maven OR mvn) commands"
    assert _best_score(searcher, typed, "all_three") <= _best_score(searcher, typed, "mvn_commands")


def test_too_many_nearness_alternatives_are_refused_with_the_limit() -> None:
    groups = " ".join("(" + " OR ".join(f"w{g}x{i}" for i in range(5)) + ")" for g in range(3))
    with pytest.raises(QuerySyntaxError, match="64"):
        QueryPlan.from_user_text(f"{{5}} {groups}")
