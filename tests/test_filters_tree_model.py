"""The filter set as tree branches, and back — the model behind the browser."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from fnd.filters import FilterSpec
from fnd.filters.scan import SourceSample, sample_source
from fnd.filters.tree_model import apply_selection, selection_for, spec_branches


def _sample() -> SourceSample:
    return SourceSample(
        kinds={"md": 439, "pdf": 7},
        tags={"os": {"no_index": 3, "wk3": 2}, "frontmatter": {"private": 15}},
    )


class TestBranches:
    def test_only_kinds_present_are_offered(self) -> None:
        """A picker listing types the source does not contain is noise."""
        labels = {
            item[0]
            for branch in spec_branches(FilterSpec(), _sample())
            for item in branch.items
            if item[0].startswith("kind:")
        }
        assert labels == {"kind:md", "kind:pdf"}

    def test_every_kind_is_offered_when_nothing_is_known(self) -> None:
        branches = spec_branches(FilterSpec(), None)
        kinds = {i[0] for b in branches for i in b.items if i[0].startswith("kind:")}
        assert len(kinds) > 10

    def test_tag_branch_cycles_and_others_do_not(self) -> None:
        modes = {b.id: b.mode for b in spec_branches(FilterSpec(), _sample())}
        assert modes["tags"] == "cycle"
        assert modes["ignore"] == "multi"
        assert modes["size"] == "radio"

    def test_an_active_tag_sorts_first(
        self,
    ) -> None:
        """What is switched on must be visible without scrolling the corpus."""
        spec = FilterSpec(exclude_tags=("wk3",))
        tags = next(b for b in spec_branches(spec, _sample()) if b.id == "tags")
        assert tags.items[0][0] == "tag:wk3"

    def test_a_configured_tag_absent_from_the_sample_still_appears(self) -> None:
        spec = FilterSpec(exclude_tags=("never_scanned",))
        tags = next(b for b in spec_branches(spec, _sample()) if b.id == "tags")
        assert any(i[0] == "tag:never_scanned" for i in tags.items)


class TestSelectionRoundTrip:
    @pytest.mark.parametrize(
        "spec",
        [
            FilterSpec(),
            FilterSpec(exclude_tags=("no_index",)),
            FilterSpec(kinds=("md", "pdf")),
            FilterSpec(max_size=50_000_000),
            FilterSpec(kinds=("md",), exclude_tags=("no_index",), max_size=1_000_000),
        ],
    )
    def test_a_spec_survives_the_tree(self, spec: FilterSpec) -> None:
        selected, excluded = selection_for(spec, gitignore=True, fndignore=False)
        back, git, fnd = apply_selection(spec, selected, excluded)
        assert back == spec
        assert git is True
        assert fnd is False

    def test_a_date_window_resolves_to_a_fixed_bound(self) -> None:
        """A rolling window would change what the index holds as time passes."""
        spec, _git, _fnd = apply_selection(FilterSpec(), {"modified:30"}, set())
        assert spec.modified_after == dt.date.today() - dt.timedelta(days=30)

    def test_choosing_any_clears_the_bound(self) -> None:
        start = FilterSpec(max_size=50_000_000)
        spec, _g, _f = apply_selection(start, {"size:any"}, set())
        assert spec.max_size is None

    def test_the_expression_is_left_alone(self) -> None:
        """The tree edits the rows; anything it cannot show must survive."""
        start = FilterSpec(expression="file.name ~~ 'draft-*'", frontmatter="Course == 'X'")
        spec, _g, _f = apply_selection(start, {"size:any"}, set())
        assert spec.expression == "file.name ~~ 'draft-*'"
        assert spec.frontmatter == "Course == 'X'"


class TestBoundedScan:
    def test_it_finds_kinds_and_tags(self, tmp_path: Path) -> None:
        (tmp_path / "a.md").write_text("---\ntags: [alpha]\n---\n", encoding="utf-8")
        (tmp_path / "b.txt").write_text("x", encoding="utf-8")
        got = sample_source(tmp_path)
        assert got.kinds == {"md": 1, "txt": 1}
        assert got.tags.get("frontmatter", {}).get("alpha") == 1

    def test_it_records_frontmatter_keys(self, tmp_path: Path) -> None:
        (tmp_path / "a.md").write_text("---\nCourse: DPwC\n---\n", encoding="utf-8")
        assert "Course" in sample_source(tmp_path).frontmatter_keys

    def test_the_file_budget_truncates_rather_than_running_on(self, tmp_path: Path) -> None:
        for i in range(30):
            (tmp_path / f"n{i}.md").write_text("x", encoding="utf-8")
        got = sample_source(tmp_path, max_files=5)
        assert got.files_seen == 5
        assert got.truncated is True

    def test_a_malformed_note_does_not_stop_the_scan(self, tmp_path: Path) -> None:
        (tmp_path / "bad.md").write_text("---\n  nope\n---\n", encoding="utf-8")
        (tmp_path / "good.md").write_text("---\ntags: [ok]\n---\n", encoding="utf-8")
        got = sample_source(tmp_path)
        assert got.files_seen == 2
        assert got.tags.get("frontmatter", {}).get("ok") == 1
