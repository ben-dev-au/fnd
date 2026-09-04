"""The text view of a filter set, and the way back.

The guarantee is that the filter behaves the same after a round-trip, not that
a value stays in the field it started in: a clause typed as free text that
matches a row's shape is *meant* to become that row.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from fnd.file_facts import FileFacts
from fnd.filter_dsl import FilterError
from fnd.filters import FilterSpec, build_gate
from fnd.filters.text_form import parse, parse_or_error, render


def _corpus(root: Path) -> list[Path]:
    """A spread of kinds, sizes and dates for behavioural comparison."""
    made: list[Path] = []
    for name, body in (
        ("a.md", "x"),
        ("b.md", "x" * 400),
        ("c.txt", "x" * 40),
        ("d.pdf", "%PDF-1.4\n"),
        ("sub/e.md", "x" * 4000),
    ):
        p = root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
        made.append(p)
    return made


def _admits(spec: FilterSpec, files: list[Path], root: Path) -> set[str]:
    gate = build_gate(spec)
    return {f.name for f in files if gate.passes(FileFacts(f, root=root))}


class TestRoundTripBehaviour:
    @settings(max_examples=100, deadline=None)
    @given(
        kinds=st.lists(st.sampled_from(["pdf", "md", "txt"]), unique=True, max_size=3),
        tags=st.lists(
            st.text(alphabet="abcdefghijklmnopqrstuvwxyz_", min_size=1, max_size=6),
            unique=True,
            max_size=2,
        ),
        min_size=st.one_of(st.none(), st.integers(min_value=1, max_value=1000)),
        max_size=st.one_of(st.none(), st.integers(min_value=1, max_value=10_000)),
        modified_after=st.one_of(st.none(), st.dates()),
    )
    def test_a_rendered_spec_parses_back_to_the_same_fields(
        self,
        kinds: list[str],
        tags: list[str],
        min_size: int | None,
        max_size: int | None,
        modified_after: dt.date | None,
    ) -> None:
        spec = FilterSpec(
            kinds=tuple(kinds),
            exclude_tags=tuple(tags),
            min_size=min_size,
            max_size=max_size,
            modified_after=modified_after,
        )
        assert parse(render(spec)) == spec

    def test_an_empty_spec_round_trips(self) -> None:
        assert render(FilterSpec()) == ""
        assert parse("") == FilterSpec()

    def test_frontmatter_survives_with_its_kind_scope(self) -> None:
        """The scope the compiler applies is written out, not silently dropped."""
        spec = FilterSpec(frontmatter="Course == 'DPwC'")
        text = render(spec)
        assert "file.kind in [" in text, text
        assert parse(text).frontmatter == "Course == 'DPwC'"

    def test_an_unrecognised_clause_is_kept(self) -> None:
        spec = parse("file.name ~~ 'draft-*'")
        assert spec.expression == "file.name ~~ 'draft-*'"
        assert parse(render(spec)) == spec

    def test_several_unrecognised_clauses_are_all_kept(self) -> None:
        spec = parse("(file.name ~~ 'a*') AND (file.ext == '.md')")
        kept = [spec.expression, *spec.raw]
        assert len(kept) == 2, kept


class TestTextInformsTheRows:
    """Typing a row-shaped clause is how the UI picks it up — not a defect."""

    def test_a_typed_kind_clause_becomes_the_kinds_row(self) -> None:
        assert parse("file.kind in ['pdf', 'md']").kinds == ("pdf", "md")

    def test_a_typed_tag_clause_becomes_the_tags_row(self) -> None:
        assert parse("NOT ('no_index' in file.tags.all)").exclude_tags == ("no_index",)

    def test_a_single_provider_survives_as_an_expression(self) -> None:
        """The row means any source; naming one is still the user's to keep."""
        spec = parse("NOT ('no_index' in file.tags.os)")
        assert spec.exclude_tags == ()
        assert "file.tags.os" in spec.expression

    def test_a_typed_size_clause_becomes_the_size_row(self) -> None:
        spec = parse("file.size <= 50_000_000")
        assert spec.max_size == 50_000_000
        assert spec.expression == ""


class TestSameBehaviourAfterRoundTrip:
    @pytest.mark.parametrize(
        "spec",
        [
            FilterSpec(kinds=("md",)),
            FilterSpec(max_size=100),
            FilterSpec(min_size=10, max_size=1000),
            FilterSpec(kinds=("md", "txt"), max_size=500),
            FilterSpec(expression="file.name ~~ '*.md'"),
            FilterSpec(kinds=("md",), expression="file.size > 100"),
        ],
    )
    def test_the_gate_admits_the_same_files(self, tmp_path: Path, spec: FilterSpec) -> None:
        files = _corpus(tmp_path)
        assert _admits(parse(render(spec)), files, tmp_path) == _admits(spec, files, tmp_path)


class TestSafety:
    def test_an_or_clause_cannot_capture_its_neighbours(self, tmp_path: Path) -> None:
        """Without per-clause brackets a raw OR widens the whole filter."""
        files = _corpus(tmp_path)
        spec = FilterSpec(kinds=("pdf",), expression="file.size == 1 OR file.size == 40")
        assert _admits(parse(render(spec)), files, tmp_path) == _admits(spec, files, tmp_path)
        assert _admits(spec, files, tmp_path) == set()

    def test_malformed_text_reports_a_column_rather_than_raising(self) -> None:
        spec, err = parse_or_error("file.kind in [")
        assert spec is None
        assert err is not None
        assert err.column >= 1

    def test_parse_raises_for_callers_that_want_it(self) -> None:
        with pytest.raises(FilterError):
            parse("file.kind in [")


class TestValuesCarryingAQuote:
    """The text form is user-facing, so it must not mangle a real tag."""

    @pytest.mark.parametrize(
        "tag", ["don't-index", "has,comma", "has space", "back\\slash", "both'\\x"]
    )
    def test_a_tag_round_trips_verbatim(self, tag: str) -> None:
        spec = FilterSpec(exclude_tags=(tag,))
        assert parse(render(spec)).exclude_tags == (tag,)


class TestFieldNamesCarryingAQuote:
    """``_field`` must escape the same way ``_value`` does, or a quoted field
    name re-emits as text that will not parse back."""

    def test_a_field_name_with_a_double_quote_round_trips(self) -> None:
        spec = FilterSpec(expression='"od\\"d" == 1')
        assert parse(render(spec)).expression == '"od\\"d" == 1'


class TestIncludeTags:
    def test_one_included_tag_round_trips(self) -> None:
        spec = FilterSpec(include_tags=("readings",))
        assert render(spec) == "'readings' in file.tags.all"
        assert parse(render(spec)).include_tags == ("readings",)

    def test_several_are_one_or_clause(self) -> None:
        """Carrying any of them is enough, so they must not become AND."""
        spec = FilterSpec(include_tags=("a", "b", "c"))
        assert parse(render(spec)).include_tags == ("a", "b", "c")

    def test_include_and_exclude_survive_together(self) -> None:
        spec = FilterSpec(include_tags=("keep",), exclude_tags=("drop",))
        back = parse(render(spec))
        assert back.include_tags == ("keep",)
        assert back.exclude_tags == ("drop",)

    def test_an_or_of_something_else_stays_an_expression(self) -> None:
        """Only a pure tag-membership OR is a row; anything else is raw text."""
        spec = parse("'a' in file.tags.all OR file.size > 10")
        assert spec.include_tags == ()
        assert "file.size" in spec.expression
