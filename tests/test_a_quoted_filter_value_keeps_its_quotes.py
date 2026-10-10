"""Typographic and full-width quotes are quote syntax wherever a typed query reads
quotes, while a filter value keeps every character as typed, apostrophes too.
Rule text outside a query (config, the filter rows) keeps ASCII quote syntax."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from fnd.filter_dsl import compile_filter
from fnd.filters.model import FilterSpec
from fnd.filters.text_form import parse, render
from fnd.index import build_index
from fnd.query import Searcher
from fnd.query_plan import QueryPlan
from fnd.text_canon import quote_view


@pytest.mark.parametrize("typed", ["＂a [b] c＂", "“a [b] c”", '"a [b] c"'])
def test_brackets_inside_any_quotes_stay_search_text(typed: str) -> None:
    plan = QueryPlan.from_user_text(typed)
    assert plan.metadata_filter is None
    assert plan.lexical == '"a [b] c"'


@pytest.mark.parametrize(
    ("typed", "fields"),
    [
        ("[Title == 'Ben’s notes']", {"Title": "Ben’s notes"}),
        ("[Title == ‘Ben’s notes’]", {"Title": "Ben’s notes"}),
        ("[Type == ‘Meeting’]", {"Type": "Meeting"}),
        ("[Type == 'Meeting’]", {"Type": "Meeting"}),
        ("[Author == 'O＇Neil']", {"Author": "O＇Neil"}),
        ("[Title == 'the “best” plan']", {"Title": "the “best” plan"}),
        ("[“Due Date” < 2026-01-01]", {"Due Date": dt.date(2025, 6, 1)}),
        (r"[Title == ‘it\’s’]", {"Title": "it's"}),
        (r"[Title == ‘C:\dir\’]", {"Title": "C:\\dir\\"}),
    ],
)
def test_a_query_filter_reads_quotes_and_keeps_its_value_as_typed(
    typed: str, fields: dict[str, object]
) -> None:
    rule = QueryPlan.from_user_text(typed).metadata_filter
    assert rule is not None
    assert compile_filter(rule)(fields)


def test_a_config_rule_keeps_ascii_quote_syntax() -> None:
    """A rule that loaded before still loads: only ASCII quotes delimit it."""
    assert compile_filter("Owner == 'the students’'")({"Owner": "the students’"})


@pytest.mark.parametrize("tag", ["students’", "‘draft’", "rock ‘n’ roll", "x＇"])
def test_a_rendered_tag_rule_parses_back(tag: str) -> None:
    spec = FilterSpec(include_tags={"frontmatter": (tag,)})
    assert parse(render(spec)).include_tags == spec.include_tags


def test_the_view_keeps_every_position() -> None:
    typed = "‘Ben’s’ “x” ＂y＂ O＇Neil ’tis"
    view = quote_view(typed)
    assert len(view) == len(typed)
    assert view == '\'Ben’s\' "x" "y" O＇Neil \'tis'


def test_a_note_is_found_by_a_value_with_an_apostrophe(tmp_path: Path, tmp_index_dir: Path) -> None:
    root = tmp_path / "work"
    root.mkdir()
    (root / "a.md").write_text("---\nTitle: Ben’s notes\n---\n\nbudget\n", encoding="utf-8")
    (root / "b.md").write_text("---\nTitle: Other notes\n---\n\nbudget\n", encoding="utf-8")
    build_index(roots=[root], index_dir=tmp_index_dir, collection="work")
    plan = QueryPlan.from_user_text("budget [Title == 'Ben’s notes']")
    hits = Searcher(index_dir=tmp_index_dir).search(
        plan.lexical, limit=10, metadata_filter=plan.metadata_filter
    )
    assert {Path(h.path).name for h in hits} == {"a.md"}
