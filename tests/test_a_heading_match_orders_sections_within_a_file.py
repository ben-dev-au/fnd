"""A section whose heading matches the query leads its file, while headings play no
part in which files rank first."""

from __future__ import annotations

from pathlib import Path

import pytest

import fnd.layered
from fnd.index import build_index
from fnd.layered import search_layered
from fnd.query import Searcher

_OTHER = "A note about something else entirely, in an ordinary sentence."
_GARDEN = (
    "# Garden\n\n## Watering\n\nThe budget, the budget and the budget cover the hoses.\n\n"
    "## Budget\n\nThe budget covers seeds and tools for the beds.\n"
)


def _index(tmp_path: Path, index_dir: Path, files: dict[str, str]) -> Searcher:
    root = tmp_path / "notes"
    root.mkdir()
    for name, text in files.items():
        (root / name).write_text(text, encoding="utf-8")
    for i in range(30):
        (root / f"other{i}.md").write_text(f"# Other {i}\n\n{_OTHER}\n", encoding="utf-8")
    build_index(roots=[root], index_dir=index_dir, collection="notes")
    return Searcher(index_dir=index_dir)


@pytest.fixture
def notes(tmp_path: Path, tmp_index_dir: Path) -> Searcher:
    return _index(
        tmp_path,
        tmp_index_dir,
        {
            "garden.md": _GARDEN,
            "holiday.md": "# Holiday\n\n## Budget\n\nFlights booked.\n\n## Plans\n\nThe budget is tight.\n",
            "invoices.md": "# Invoices\n\n## March\n\nThe budget, the budget and the budget again.\n",
        },
    )


def _sections(searcher: Searcher, query: str, per_file: int = 10) -> dict[str, list[str]]:
    groups = search_layered(searcher, query=query, limit=40, sections_per_file=per_file)
    return {Path(g.path).name: [h.heading_path for h in g.hits] for g in groups}


def test_the_section_whose_heading_matches_leads_its_file(notes: Searcher) -> None:
    assert _sections(notes, "budget")["garden.md"][0] == "Garden > Budget"


def test_without_the_heading_weight_the_better_body_match_leads(
    notes: Searcher, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fnd.layered, "SECTION_HEADING_BOOST", 0.0)
    assert _sections(notes, "budget")["garden.md"][0] == "Garden > Watering"


def test_one_section_per_file_still_shows_the_heading_match(notes: Searcher) -> None:
    assert _sections(notes, "budget", per_file=1)["garden.md"] == ["Garden > Budget"]


def test_a_heading_match_does_not_rank_its_file_higher(notes: Searcher) -> None:
    files = list(_sections(notes, "budget"))
    assert files.index("invoices.md") < files.index("holiday.md")


def test_unshown_sections_of_a_titled_file_do_not_crowd_out_the_lift(
    tmp_path: Path, tmp_index_dir: Path
) -> None:
    months = "".join(f"## Month {i}\n\nNothing to report this month at all.\n\n" for i in range(20))
    searcher = _index(
        tmp_path,
        tmp_index_dir,
        {
            "annual.md": f"# Budget\n\n{months}## Summary\n\nThe budget held.\n\n"
            "## Review\n\nThe budget slipped.\n",
            "garden.md": _GARDEN.replace("# Garden", "# Garden Notes Today"),
        },
    )
    assert _sections(searcher, "budget")["garden.md"][0] == "Garden Notes Today > Budget"


def test_a_cascade_keeps_its_exact_section_above_a_looser_one(
    tmp_path: Path, tmp_index_dir: Path
) -> None:
    searcher = _index(
        tmp_path,
        tmp_index_dir,
        {
            "lab.md": "# Lab\n\n## Zorblax\n\nThe zorblax device.\n\n"
            "## Notes\n\nWe wrote about the zorblax device in a rather long "
            "sentence with many other ordinary words padding it out for length.\n\n"
            "## Other\n\nZorblux zorblux zorblux.\n",
            "tips.md": "# Tips\n\n## Zorblax\n\nThe zorblax again.\n\n## Misc\n\nOne more zorblax.\n",
        },
    )
    groups, trace = search_layered(
        searcher, query="zorblax", limit=40, sections_per_file=10, with_trace=True
    )
    assert trace.regime.startswith("cascade"), "the fixture no longer reaches the cascade"
    lab = next(g for g in groups if g.path.endswith("lab.md"))
    order = [h.heading_path for h in lab.hits]
    assert order.index("Lab > Notes") < order.index("Lab > Other")


def test_heading_scores_cover_only_the_sections_asked_about(notes: Searcher) -> None:
    groups = search_layered(notes, query="budget", limit=40, sections_per_file=10)
    garden = next(g for g in groups if g.path.endswith("garden.md"))
    asked = {(h.parent_id, h.chunk_seq) for h in garden.hits}
    scores = notes.heading_scores("budget", asked)
    assert scores
    assert set(scores) <= asked


@pytest.mark.parametrize("query", ["budg*", "budgte~", "/budg.*/"])
def test_a_pattern_word_has_no_heading_score(notes: Searcher, query: str) -> None:
    groups = search_layered(notes, query="budget", limit=40, sections_per_file=10)
    asked = {(h.parent_id, h.chunk_seq) for g in groups for h in g.hits}
    assert notes.heading_scores(query, asked) == {}


def test_a_respelt_word_still_lifts_its_heading(notes: Searcher) -> None:
    assert _sections(notes, "budgte")["garden.md"][0] == "Garden > Budget"


_COURSE = (
    "# Course\n\n## Learning activity\n\nOne learning task.\n\n"
    "## Approaches\n\nDeep learning and deep learning again, set out in a longer "
    "paragraph of ordinary words about the methods used in the course.\n"
)


@pytest.fixture
def course(tmp_path: Path, tmp_index_dir: Path) -> Searcher:
    return _index(
        tmp_path,
        tmp_index_dir,
        {"course.md": _COURSE, "deep.md": "# Sea\n\n## Trench\n\nThe deep trench.\n"},
    )


def test_a_section_holding_every_word_leads_a_heading_holding_one(course: Searcher) -> None:
    assert _sections(course, "deep learning")["course.md"][0] == "Course > Approaches"


def test_without_the_every_word_tier_the_partial_heading_leads(
    course: Searcher, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Searcher, "holding_every_word", lambda self, query, sections: set())
    assert _sections(course, "deep learning")["course.md"][0] == "Course > Learning activity"


def test_an_excluded_word_does_not_count_as_wanted(course: Searcher) -> None:
    groups = search_layered(course, query="deep learning", limit=40, sections_per_file=10)
    asked = {(h.parent_id, h.chunk_seq) for g in groups for h in g.hits}
    assert course.holding_every_word("learning -deep", asked) == set()
    assert course.holding_every_word("deep learning", asked)
