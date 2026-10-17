"""A section whose heading matches the query leads its file, while headings play no
part in which files rank first."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import fnd.layered
from fnd.analysis import analyse
from fnd.index import build_index
from fnd.layered import search_layered
from fnd.query import Hit, Searcher
from fnd.schema import HEADING_TIE_BREAKER, PARENT_HEADING_WEIGHT, SECTION_HEADING_BOOST

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
    groups = search_layered(searcher, query="budget", limit=40, sections_per_file=10)
    garden = {h.heading_path: h for g in groups if g.path.endswith("garden.md") for h in g.hits}
    budget = garden["Garden Notes Today > Budget"]
    assert budget.section_score is not None
    assert budget.section_score > budget.score


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


def test_word_evidence_covers_only_the_sections_asked_about(notes: Searcher) -> None:
    groups = search_layered(notes, query="budget", limit=40, sections_per_file=10)
    garden = next(g for g in groups if g.path.endswith("garden.md"))
    asked = {(h.parent_id, h.chunk_seq) for h in garden.hits}
    e = notes.word_evidence(notes.wanted_tokens("budget"), asked)[analyse("budget")[0]]
    assert e.heading
    assert set(e.body) | set(e.heading) <= asked


@pytest.mark.parametrize("query", ["budg*", "budgte~", "/budg.*/"])
def test_a_pattern_word_is_not_a_wanted_word(notes: Searcher, query: str) -> None:
    assert notes.wanted_tokens(query) == []


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


def test_without_the_coverage_scale_the_partial_heading_leads(
    course: Searcher, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fnd.layered, "COVERAGE_POWER", 0.0)
    assert _sections(course, "deep learning")["course.md"][0] == "Course > Learning activity"


def test_an_excluded_word_does_not_count_as_wanted(course: Searcher) -> None:
    assert course.wanted_tokens("learning -deep") == analyse("learning")
    assert course.wanted_tokens("deep learning") == analyse("deep learning")


def _lift(searcher: Searcher, query: str, h: Hit, share: float) -> float:
    k = (h.parent_id, h.chunk_seq)
    total = 0.0
    for e in searcher.word_evidence(searcher.wanted_tokens(query), [k]).values():
        lifted, plain = SECTION_HEADING_BOOST * share * e.heading.get(k, 0.0), e.body.get(k, 0.0)
        total += max(0.0, lifted - plain) + HEADING_TIE_BREAKER * min(lifted, plain)
    return total


def _plans(tmp_path: Path, tmp_index_dir: Path, text: str) -> tuple[Searcher, dict[str, Hit]]:
    searcher = _index(tmp_path, tmp_index_dir, {"plans.md": text})
    groups = search_layered(searcher, query="budget", limit=40, sections_per_file=10)
    return searcher, {
        h.heading_path: h for g in groups if g.path.endswith("plans.md") for h in g.hits
    }


def test_a_parent_heading_lifts_its_subsections_by_a_share(
    tmp_path: Path, tmp_index_dir: Path
) -> None:
    searcher, hits = _plans(
        tmp_path,
        tmp_index_dir,
        "# Plans\n\n## Budget\n\nThe budget overview.\n\n"
        "### Timeline\n\nDates first, and the budget once, among other plain words.\n\n"
        "## Costs\n\nThe budget, the budget and the budget again.\n",
    )
    own, child = hits["Plans > Budget"], hits["Plans > Budget > Timeline"]
    assert own.section_score == pytest.approx(own.score + _lift(searcher, "budget", own, 1.0))
    child_lift = _lift(searcher, "budget", child, PARENT_HEADING_WEIGHT)
    assert child.section_score == pytest.approx(child.score + child_lift)


def test_a_heading_over_every_section_counts_alike_for_each(
    tmp_path: Path, tmp_index_dir: Path
) -> None:
    searcher, hits = _plans(
        tmp_path,
        tmp_index_dir,
        "# Budget\n\nThe budget overview.\n\n"
        "## Timeline\n\nDates first, and the budget once, among other plain words.\n",
    )
    child = hits["Budget > Timeline"]
    assert child.section_score == pytest.approx(child.score + _lift(searcher, "budget", child, 1.0))


def test_a_parent_heading_keeps_its_share_when_every_match_sits_under_it(
    tmp_path: Path, tmp_index_dir: Path
) -> None:
    """Only the leading heading counts alike; a shared H2 is still a parent."""
    searcher, hits = _plans(
        tmp_path,
        tmp_index_dir,
        "# Plans\n\n## Budget\n\nThe budget overview.\n\n"
        "### Timeline\n\nDates first, and the budget once, among other plain words.\n\n"
        "## Costs\n\nSeeds, tools and hoses.\n",
    )
    child = hits["Plans > Budget > Timeline"]
    child_lift = _lift(searcher, "budget", child, PARENT_HEADING_WEIGHT)
    assert child.section_score == pytest.approx(child.score + child_lift)


def test_a_file_with_one_matching_section_still_shows_its_score(notes: Searcher) -> None:
    """Coverage is judged on every shown section, not only those of files with several."""
    groups = search_layered(notes, query="budget again", limit=40, sections_per_file=10)
    (march,) = next(g for g in groups if g.path.endswith("invoices.md")).hits
    assert march.section_score == pytest.approx(march.score)


def _big_file_shown(tmp_path: Path, tmp_index_dir: Path) -> tuple[Searcher, Any, dict[Any, Any]]:
    body = "".join(f"## Part {i}\n\nThe budget for part {i}.\n\n" for i in range(200))
    searcher = _index(tmp_path, tmp_index_dir, {"big.md": f"# Big\n\n{body}"})
    raw = searcher._searcher  # pyright: ignore[reportPrivateUsage]

    class Counting:
        reads = 0

        def __getattr__(self, name: str) -> object:
            return getattr(raw, name)

        def doc(self, address: Any) -> object:
            Counting.reads += 1
            return raw.doc(address)

    searcher._searcher = Counting()  # type: ignore[assignment]  # pyright: ignore[reportPrivateUsage]
    groups = search_layered(searcher, query="budget", limit=40, sections_per_file=3)
    shown = {(h.parent_id, h.chunk_seq): h.at for g in groups for h in g.hits}
    Counting.reads = 0
    return searcher, Counting, shown


def test_word_evidence_reads_no_document_for_sections_already_found(
    tmp_path: Path, tmp_index_dir: Path
) -> None:
    """Shown sections carry their index address, so a big file's other sections holding
    the word are skipped unread, and the scope stays one query on file ids."""
    searcher, counting, shown = _big_file_shown(tmp_path, tmp_index_dir)
    (e,) = searcher.word_evidence(searcher.wanted_tokens("budget"), shown).values()
    assert counting.reads == 0
    assert set(e.body) == set(shown)


def test_a_section_without_an_address_costs_one_read_not_the_whole_file(
    tmp_path: Path, tmp_index_dir: Path
) -> None:
    """A shown section found another way (no :attr:`Hit.at`) is looked up exactly, alone."""
    searcher, counting, shown = _big_file_shown(tmp_path, tmp_index_dir)
    first = next(iter(shown))
    shown[first] = None
    (e,) = searcher.word_evidence(searcher.wanted_tokens("budget"), shown).values()
    assert counting.reads == 1
    assert set(e.body) == set(shown)


def test_sections_given_without_addresses_read_only_themselves(
    tmp_path: Path, tmp_index_dir: Path
) -> None:
    """Plain section keys cost one read each, never one per section of their files."""
    searcher, counting, shown = _big_file_shown(tmp_path, tmp_index_dir)
    (e,) = searcher.word_evidence(searcher.wanted_tokens("budget"), set(shown)).values()
    assert counting.reads == len(shown)
    assert set(e.body) == set(shown)
