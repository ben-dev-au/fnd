"""Coverage of the filter space itself, rather than of chosen examples.

Three layers, each closed over the schema so a new filter field is included
automatically and fails until it is handled:

* resolution: what a source override does to each field of the defaults;
* behaviour: each field actually changes which files the walker yields;
* invariants: properties that must hold for any combination of filters.
"""

from __future__ import annotations

import datetime as dt
import os
from pathlib import Path
from typing import Any, ClassVar

import pytest

from fnd.config import CLEARABLE, DefaultFilters, SourceConfig, SourceFilters, resolve_filters
from fnd.tui.settings_screen import _source_filters_or_none
from fnd.walk import walk_sources

#: Per field: a value for the defaults, and a different one for a source to
#: override it with. Every field must appear; the first test enforces that.
VALUES: dict[str, tuple[Any, Any]] = {
    "respect_gitignore": (True, False),
    "respect_fndignore": (True, False),
    "include_tags": (["keep"], ["other"]),
    "exclude_tags": (["drop"], ["nope"]),
    "kinds": (["md"], ["pdf"]),
    "min_size": (10, 20),
    "max_size": (1000, 2000),
    "created_after": (dt.date(2020, 1, 1), dt.date(2021, 1, 1)),
    "created_before": (dt.date(2030, 1, 1), dt.date(2031, 1, 1)),
    "modified_after": (dt.date(2020, 6, 1), dt.date(2021, 6, 1)),
    "modified_before": (dt.date(2030, 6, 1), dt.date(2031, 6, 1)),
    "frontmatter": ("Course == 'A'", "Course == 'B'"),
    "expression": ("file.size > 1", "file.size > 2"),
}

#: Fields whose empty value means "override to nothing", not "unset".
EMPTIABLE = ("include_tags", "exclude_tags", "kinds")


class TestEveryFieldResolves:
    """`resolve_filters` decides what a source actually indexes. Before this,
    seven of the thirteen fields had no override test at all."""

    def test_the_table_covers_every_field(self) -> None:
        """Closed over the schema: a new filter field fails here until it is
        given values, rather than silently going untested. `clears` is not a
        filter but the mechanism for dropping one, and has its own tests."""
        assert set(VALUES) == set(SourceFilters.model_fields) - {"clears"}
        assert set(VALUES) == set(DefaultFilters.model_fields)

    @pytest.mark.parametrize("field", sorted(CLEARABLE))
    def test_a_clearable_field_drops_the_inherited_value(self, field: str) -> None:
        """A list clears with `[]` and a string with `""`; these six had no
        such value, so the choice silently reverted."""
        base, _ = VALUES[field]
        resolved = resolve_filters(
            SourceFilters.model_validate({"clears": [field]}),
            DefaultFilters.model_validate({field: base}),
        )
        assert getattr(resolved, field) is None

    def test_only_the_six_are_clearable(self) -> None:
        """A bool says it with `false` and a list with `[]`; naming those would
        be a second way to say one thing."""
        with pytest.raises(ValueError, match="clears only applies"):
            SourceFilters.model_validate({"clears": ["respect_gitignore"]})

    @pytest.mark.parametrize("field", sorted(VALUES))
    def test_an_unset_field_inherits_the_default(self, field: str) -> None:
        base, _ = VALUES[field]
        defaults = DefaultFilters.model_validate({field: base})
        resolved = resolve_filters(SourceFilters(), defaults)
        assert getattr(resolved, field) == base

    @pytest.mark.parametrize("field", sorted(VALUES))
    def test_a_set_field_overrides_the_default(self, field: str) -> None:
        base, override = VALUES[field]
        defaults = DefaultFilters.model_validate({field: base})
        resolved = resolve_filters(SourceFilters.model_validate({field: override}), defaults)
        assert getattr(resolved, field) == override

    @pytest.mark.parametrize("field", EMPTIABLE)
    def test_an_empty_field_overrides_to_nothing(self, field: str) -> None:
        """`None` inherits and `[]` opts out; conflating them silently keeps a
        rule the user switched off for this source."""
        base, _ = VALUES[field]
        defaults = DefaultFilters.model_validate({field: base})
        resolved = resolve_filters(SourceFilters.model_validate({field: []}), defaults)
        assert not getattr(resolved, field)

    @pytest.mark.parametrize("field", sorted(VALUES))
    def test_resolution_does_not_disturb_other_fields(self, field: str) -> None:
        _, override = VALUES[field]
        defaults = DefaultFilters()
        resolved = resolve_filters(SourceFilters.model_validate({field: override}), defaults)
        for other in VALUES:
            if other != field:
                assert getattr(resolved, other) == getattr(defaults, other), other


def _corpus(root: Path) -> None:
    """A tree where every filter below discriminates: `keep.md` survives each
    filter and `drop.*` is what that filter removes."""
    # Comfortably above min_size and below max_size, so neither bound removes
    # the file every case relies on surviving.
    body = "body here. " * 30
    (root / "keep.md").write_text(f"---\nCourse: A\ntags: [keep]\n---\n{body}\n", encoding="utf-8")
    (root / "drop.pdf").write_bytes(b"%PDF-1.4\n" + b"x" * 400)
    (root / "tiny.md").write_text("---\nCourse: A\ntags: [keep]\n---\n", encoding="utf-8")
    (root / "huge.md").write_text(
        "---\nCourse: A\ntags: [keep]\n---\n" + "y" * 5000, encoding="utf-8"
    )
    (root / "other.md").write_text(
        "---\nCourse: B\ntags: [nope]\n---\nbody here\n", encoding="utf-8"
    )
    (root / ".gitignore").write_text("ignored.md\n", encoding="utf-8")
    (root / ".fndignore").write_text("fndignored.md\n", encoding="utf-8")
    for name in ("ignored.md", "fndignored.md"):
        (root / name).write_text("---\nCourse: A\ntags: [keep]\n---\nbody here\n", encoding="utf-8")
    old = dt.datetime(2015, 1, 1).timestamp()
    (root / "old.md").write_text("---\nCourse: A\ntags: [keep]\n---\nbody here\n", encoding="utf-8")
    os.utime(root / "old.md", (old, old))


#: Per field: the value to apply, and the file it must remove from the walk.
#: Tag rules read a note's YAML `tags:`, which works on every platform.
REMOVES: dict[str, tuple[Any, str | None]] = {
    "respect_gitignore": (True, "ignored.md"),
    "respect_fndignore": (True, "fndignored.md"),
    "include_tags": (["keep"], "other.md"),
    "exclude_tags": (["nope"], "other.md"),
    "kinds": (["md"], "drop.pdf"),
    "min_size": (100, "tiny.md"),
    "max_size": (4000, "huge.md"),
    "modified_after": (dt.date(2018, 1, 1), "old.md"),
    "modified_before": (dt.date(2100, 1, 1), None),
    "created_after": (dt.date(1990, 1, 1), None),
    "created_before": (dt.date(2100, 1, 1), None),
    "frontmatter": ("Course == 'A'", "other.md"),
    "expression": ("file.name != 'other.md'", "other.md"),
}


def _walked(root: Path, filters: DefaultFilters, source: SourceFilters | None = None) -> set[str]:
    """Stamps the resolved filters the way Config does. This covers
    `resolve_filters` and the walk; that Config performs the stamping is
    covered by TestConfigStampsResolvedFilters below."""
    src = SourceConfig(path=root, filters=source)
    src._resolved_filters = resolve_filters(source, filters)
    return {p.name for p in walk_sources(sources=[src])}


class TestEveryFieldFilters:
    """Resolution alone proves nothing: a field can resolve correctly and never
    be consulted. Each one must change what the walker yields."""

    def test_the_tables_cover_every_field(self) -> None:
        """Every field is behaviourally tested somewhere: by a real file here,
        or against stubbed timestamps in TestDateFieldsFilter."""
        real = {f for f, (_, removed) in REMOVES.items() if removed}
        assert real | set(DATE_REMOVES) == set(DefaultFilters.model_fields)

    @pytest.mark.parametrize("field", sorted(f for f, (_, r) in REMOVES.items() if r))
    def test_the_default_removes_its_file(self, field: str, tmp_path: Path) -> None:
        _corpus(tmp_path)
        value, removed = REMOVES[field]
        wide = DefaultFilters(respect_gitignore=False, respect_fndignore=False, exclude_tags=[])
        before = _walked(tmp_path, wide)
        after = _walked(tmp_path, wide.model_copy(update={field: value}))
        assert removed in before, f"{removed} was not there to remove"
        assert removed not in after, f"{field} did not remove {removed}"
        assert "keep.md" in after, f"{field} removed the file it should keep"

    @pytest.mark.parametrize("field", sorted(f for f, (_, r) in REMOVES.items() if r))
    def test_a_source_override_reaches_the_walk(self, field: str, tmp_path: Path) -> None:
        """The same rule set on the source, not the defaults."""
        _corpus(tmp_path)
        value, removed = REMOVES[field]
        wide = DefaultFilters(respect_gitignore=False, respect_fndignore=False, exclude_tags=[])
        after = _walked(tmp_path, wide, SourceFilters.model_validate({field: value}))
        assert removed not in after, f"source {field} did not remove {removed}"
        assert "keep.md" in after


class TestInvariantsOverCombinations:
    """Properties that hold for any combination, which is where interaction
    bugs live."""

    @staticmethod
    def _sets(names: list[str]) -> dict[str, Any]:
        return {n: REMOVES[n][0] for n in names}

    @pytest.mark.parametrize("seed", range(24))
    def test_adding_a_filter_never_yields_more(self, seed: int, tmp_path: Path) -> None:
        """A filter can only remove. If a combination yields a file that a
        subset excluded, two rules are fighting."""
        import random

        rng = random.Random(seed)
        _corpus(tmp_path)
        pool = sorted(f for f, (_, r) in REMOVES.items() if r)
        chosen = rng.sample(pool, rng.randint(1, len(pool)))
        wide = DefaultFilters(respect_gitignore=False, respect_fndignore=False, exclude_tags=[])
        combined = _walked(tmp_path, wide.model_copy(update=self._sets(chosen)))
        for field in chosen:
            alone = _walked(tmp_path, wide.model_copy(update=self._sets([field])))
            assert combined <= alone, f"{chosen} yields what {field} alone excludes"

    @pytest.mark.parametrize("seed", range(24))
    def test_a_combination_removes_every_file_its_parts_remove(
        self, seed: int, tmp_path: Path
    ) -> None:
        """Filters are ANDed, so the combined result is the intersection."""
        import random

        rng = random.Random(seed + 100)
        _corpus(tmp_path)
        pool = sorted(f for f, (_, r) in REMOVES.items() if r)
        chosen = rng.sample(pool, rng.randint(2, len(pool)))
        wide = DefaultFilters(respect_gitignore=False, respect_fndignore=False, exclude_tags=[])
        combined = _walked(tmp_path, wide.model_copy(update=self._sets(chosen)))
        expected = set.intersection(
            *(_walked(tmp_path, wide.model_copy(update=self._sets([f]))) for f in chosen)
        )
        assert combined == expected

    @pytest.mark.parametrize("field", sorted(f for f, (_, r) in REMOVES.items() if r))
    def test_a_source_override_beats_a_stricter_default(self, field: str, tmp_path: Path) -> None:
        """The whole point of per-source filters: one source opting out of a
        rule everything else inherits."""
        _corpus(tmp_path)
        value, removed = REMOVES[field]
        wide = DefaultFilters(respect_gitignore=False, respect_fndignore=False, exclude_tags=[])
        strict = wide.model_copy(update={field: value})
        opted_out = SourceFilters.model_validate({field: _OPT_OUT[field]})
        after = _walked(tmp_path, strict, opted_out)
        assert removed in after, f"source override of {field} did not restore {removed}"

    @pytest.mark.parametrize("seed", range(16))
    def test_order_of_application_does_not_matter(self, seed: int, tmp_path: Path) -> None:
        """AND is commutative; a rule that depends on evaluation order is a bug."""
        import random

        rng = random.Random(seed + 200)
        _corpus(tmp_path)
        pool = sorted(f for f, (_, r) in REMOVES.items() if r)
        chosen = rng.sample(pool, rng.randint(2, len(pool)))
        wide = DefaultFilters(respect_gitignore=False, respect_fndignore=False, exclude_tags=[])
        forward = _walked(tmp_path, wide.model_copy(update=self._sets(chosen)))
        backward = _walked(tmp_path, wide.model_copy(update=self._sets(list(reversed(chosen)))))
        assert forward == backward


#: What a source sets to opt out of the corresponding default.
_OPT_OUT: dict[str, Any] = {
    "respect_gitignore": False,
    "respect_fndignore": False,
    "include_tags": [],
    "exclude_tags": [],
    "kinds": [],
    "min_size": 0,
    "max_size": 10_000_000,
    "modified_after": dt.date(1990, 1, 1),
    "frontmatter": "",
    "expression": "",
}


#: Birth time cannot be set portably, so the date fields are exercised against
#: the same stub the walk reads times through.
DATE_REMOVES: dict[str, tuple[Any, str]] = {
    "created_after": (dt.date(2018, 1, 1), "old.md"),
    "created_before": (dt.date(2025, 1, 1), "future.md"),
    "modified_after": (dt.date(2018, 1, 1), "old.md"),
    "modified_before": (dt.date(2025, 1, 1), "future.md"),
}

_STAMPS = {"old.md": 2015, "future.md": 2035}


class TestDateFieldsFilter:
    """Three of the four date fields had no behavioural test at all: they
    resolved correctly and nothing checked the walk consulted them."""

    @staticmethod
    def _stub(monkeypatch: pytest.MonkeyPatch) -> None:
        from fnd.fsmeta import FileTimes

        def times(path: Path) -> FileTimes:
            year = _STAMPS.get(path.name, 2020)
            stamp = int(dt.datetime(year, 6, 1).timestamp())
            return FileTimes(mtime=stamp, created=stamp, inode_changed=stamp)

        monkeypatch.setattr("fnd.file_facts.read_file_times", times)

    @pytest.mark.parametrize("field", sorted(DATE_REMOVES))
    def test_the_default_removes_its_file(
        self, field: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _corpus(tmp_path)
        (tmp_path / "future.md").write_text(
            "---\nCourse: A\ntags: [keep]\n---\nbody here\n", encoding="utf-8"
        )
        self._stub(monkeypatch)
        value, removed = DATE_REMOVES[field]
        wide = DefaultFilters(respect_gitignore=False, respect_fndignore=False, exclude_tags=[])
        before = _walked(tmp_path, wide)
        after = _walked(tmp_path, wide.model_copy(update={field: value}))
        assert removed in before
        assert removed not in after, f"{field} did not remove {removed}"
        assert "keep.md" in after

    @pytest.mark.parametrize("field", sorted(DATE_REMOVES))
    def test_a_source_override_reaches_the_walk(
        self, field: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _corpus(tmp_path)
        (tmp_path / "future.md").write_text(
            "---\nCourse: A\ntags: [keep]\n---\nbody here\n", encoding="utf-8"
        )
        self._stub(monkeypatch)
        value, removed = DATE_REMOVES[field]
        wide = DefaultFilters(respect_gitignore=False, respect_fndignore=False, exclude_tags=[])
        after = _walked(tmp_path, wide, SourceFilters.model_validate({field: value}))
        assert removed not in after, f"source {field} did not remove {removed}"
        assert "keep.md" in after


class TestConfigStampsResolvedFilters:
    """`_walked` stamps `_resolved_filters` itself, so on its own it would not
    notice Config failing to. The walk reads only what Config stamped."""

    def test_loading_resolves_each_source_against_the_defaults(self, tmp_path: Path) -> None:
        from fnd.config import Config

        config = Config.model_validate(
            {
                "defaults": {"filters": {"kinds": ["md"], "max_size": 10}},
                "collections": {
                    "c": {
                        "sources": [
                            {"path": str(tmp_path), "filters": {"max_size": 99}},
                            {"path": str(tmp_path)},
                        ]
                    }
                },
            }
        )
        overridden, inherited = config.collections["c"].sources
        assert overridden.effective_filters.max_size == 99
        assert overridden.effective_filters.kinds == ["md"], "defaults were not merged in"
        assert inherited.effective_filters.max_size == 10


class TestClearDropsOverridesNotProtections:
    """`c` on a source emptied the resolved set, which threw away the
    inherited `no_index` exclusion, so undoing a file-type filter also
    switched off the never-index opt-out, with no confirmation."""

    @staticmethod
    def _corpus(root: Path) -> None:
        (root / "public.md").write_text("---\ntags: [ok]\n---\nhello\n")
        (root / "private.md").write_text("---\ntags: [no_index]\n---\nsecret\n")
        (root / "note.txt").write_text("plain\n")

    @staticmethod
    def _recorded(spec: Any, defaults: DefaultFilters) -> dict[str, Any]:
        """What the browser's save writes for ``spec``: only what differs."""
        from fnd.tui.settings_screen import _same_setting, _spec_to_mapping

        values = _spec_to_mapping(spec)
        values["respect_gitignore"] = defaults.respect_gitignore
        values["respect_fndignore"] = defaults.respect_fndignore
        return {k: v for k, v in values.items() if not _same_setting(v, getattr(defaults, k, None))}

    def test_clearing_a_source_leaves_the_inherited_exclusion_standing(
        self, tmp_path: Path
    ) -> None:
        from fnd.tui.settings_screen import _spec_from_filters

        self._corpus(tmp_path)
        defaults = DefaultFilters(exclude_tags=["no_index"])
        inherited = _spec_from_filters(resolve_filters(SourceFilters(), defaults))
        recorded = self._recorded(inherited, defaults)
        assert recorded == {}, f"clearing to the inherited set is not an override: {recorded}"
        walked = _walked(tmp_path, defaults, _source_filters_or_none(recorded))
        assert "private.md" not in walked, walked
        assert walked == {"public.md", "note.txt"}

    def test_clearing_to_an_empty_set_would_have_readmitted_it(self, tmp_path: Path) -> None:
        """The negative control: clearing to an empty set readmits the file."""
        from fnd.filters import FilterSpec

        self._corpus(tmp_path)
        defaults = DefaultFilters(exclude_tags=["no_index"])
        recorded = self._recorded(FilterSpec(), defaults)
        assert recorded == {"exclude_tags": []}
        assert "private.md" in _walked(tmp_path, defaults, _source_filters_or_none(recorded))

    def test_the_global_defaults_still_clear_to_nothing(self) -> None:
        """They inherit from nothing, so there an empty set is the right one."""
        from fnd.filters import FilterSpec
        from fnd.tui.settings_screen import FilterBrowserScreen

        screen = FilterBrowserScreen(
            title="Index filters",
            spec=FilterSpec(exclude_tags={"os": ("no_index",)}),
            gitignore=True,
            fndignore=True,
            on_save=lambda *_a: None,
        )
        assert screen._inherited is None


class TestTheTextViewNeverWidens:
    """Round-tripping through the text form dropped a clause whenever a
    dimension appeared twice, and dropping a conjunct can only admit more.
    Reachable with no typing at all: open the text view, save, save."""

    DIMENSIONS: ClassVar[dict[str, tuple[Any, str]]] = {
        "kinds": (("md",), "file.kind in ['pdf']"),
        "min_size": (100, "file.size >= 5000"),
        "max_size": (100, "file.size <= 5000"),
        "created_after": (dt.date(2026, 1, 1), "file.created >= 2020-01-01"),
        "created_before": (dt.date(2026, 1, 1), "file.created <= 2030-01-01"),
        "modified_after": (dt.date(2026, 1, 1), "file.modified >= 2020-01-01"),
        "modified_before": (dt.date(2026, 1, 1), "file.modified <= 2030-01-01"),
    }

    @pytest.mark.parametrize("field", sorted(DIMENSIONS))
    def test_a_dimension_named_twice_keeps_both_clauses(self, field: str) -> None:
        from dataclasses import replace as _replace

        from fnd.filters import FilterSpec
        from fnd.filters.text_form import parse, render

        value, expression = self.DIMENSIONS[field]
        spec = _replace(FilterSpec(expression=expression), **{field: value})
        once = parse(render(spec))
        assert getattr(once, field) == value, "the picker's clause was dropped"
        assert render(once) == render(parse(render(once))), "not idempotent"

    def test_the_index_does_not_widen_across_a_round_trip(self, tmp_path: Path) -> None:
        from fnd.filters import FilterSpec
        from fnd.filters.text_form import parse, render

        (tmp_path / "a.md").write_text("hello\n")
        (tmp_path / "c.pdf").write_bytes(b"%PDF-1.4\n")

        def walked(spec: FilterSpec) -> set[str]:
            overrides = SourceFilters(kinds=list(spec.kinds), expression=spec.expression or None)
            return _walked(tmp_path, DefaultFilters(), overrides)

        spec = FilterSpec(kinds=("md",), expression="file.kind in ['pdf']")
        assert walked(spec) == set(), "the two clauses together admit nothing"
        assert walked(parse(render(spec))) == set(), "the round trip widened the index"


class TestATypoCannotOpenTheIndex:
    """An unknown fact is *unknown*, not false, so its rule is waived, which
    means a misspelt field admits every file instead of none, and voids the
    valid clause beside it."""

    @pytest.mark.parametrize(
        "text",
        [
            "file.sixe <= 1000",
            "(file.kind in ['md']) AND (file.sixe <= 1000)",
            "file.author == 'x'",
        ],
    )
    def test_it_is_refused_with_a_column(self, text: str) -> None:
        from fnd.filter_dsl import parse_or_error

        _pred, err = parse_or_error(text)
        assert err is not None, f"{text!r} was accepted"
        assert err.column >= 1

    @pytest.mark.parametrize(
        "text",
        [
            "file.size <= 1000",
            "'x' in file.tags.all",
            "Course == 'DPwC' AND status == 'done'",
        ],
    )
    def test_real_facts_and_frontmatter_keys_still_parse(self, text: str) -> None:
        """Frontmatter keys cannot contain a dot, so they are never mistaken
        for a fact."""
        from fnd.filter_dsl import parse_or_error

        _pred, err = parse_or_error(text)
        assert err is None, err


class TestATypedAndStaysAnAnd:
    """ "Index only files carrying any of these" is a disjunction, so merging a
    second top-level conjunct into it turned a typed AND into an OR, which
    can only admit more files."""

    def test_two_include_clauses_do_not_collapse_into_one_or(self) -> None:
        from fnd.filters.text_form import parse, render

        typed = "'keep' in file.tags.all AND 'private' in file.tags.all"
        back = parse(typed)
        assert " AND " in render(back), f"the AND became: {render(back)}"
        assert back.expression, "the second clause has to survive somewhere"

    def test_the_index_does_not_widen(self, tmp_path: Path) -> None:
        from fnd.file_facts import FileFacts
        from fnd.filters.text import build_gate
        from fnd.filters.text_form import parse

        both = tmp_path / "both.md"
        both.write_text("---\ntags: [keep, private]\n---\nx\n")
        (tmp_path / "keep.md").write_text("---\ntags: [keep]\n---\nx\n")
        (tmp_path / "private.md").write_text("---\ntags: [private]\n---\nx\n")

        from fnd.tags import providers_for

        typed = "'keep' in file.tags.all AND 'private' in file.tags.all"
        gate = build_gate(parse(typed))
        providers = providers_for("linux", ["frontmatter"])  # frontmatter tags only
        passed = {
            p.name
            for p in tmp_path.iterdir()
            if gate.passes(FileFacts(p, root=tmp_path, tag_providers=providers))
        }
        assert passed == {"both.md"}, f"the AND admitted more than both-tagged files: {passed}"

    def test_two_exclude_clauses_still_mean_neither(self) -> None:
        """`NOT a AND NOT b` is exactly what excluding both means, so that
        merge is correct and must stay."""
        from fnd.filters.text_form import parse

        back = parse("NOT ('a' in file.tags.all) AND NOT ('b' in file.tags.all)")
        assert set(back.tag_excludes["os"]) == {"a", "b"}


class TestBoundsThatCannotBothHold:
    """Contradictory bounds are accepted everywhere and index nothing,
    silently. They are decidable without touching a corpus."""

    def test_a_size_range_the_wrong_way_round_is_named(self) -> None:
        from fnd.filters import FilterSpec

        assert FilterSpec(min_size=5000, max_size=100).impossible_bounds() == (
            "size: smallest is above largest",
        )

    def test_a_backwards_date_range_is_named(self) -> None:
        from fnd.filters import FilterSpec

        spec = FilterSpec(created_after=dt.date(2026, 6, 1), created_before=dt.date(2026, 1, 1))
        assert spec.impossible_bounds() == ("created: starts after it ends",)

    def test_a_workable_range_is_not_flagged(self) -> None:
        from fnd.filters import FilterSpec

        assert FilterSpec(min_size=100, max_size=5000).impossible_bounds() == ()
        assert FilterSpec().impossible_bounds() == ()

    def test_it_really_does_index_nothing(self, tmp_path: Path) -> None:
        (tmp_path / "a.md").write_text("x" * 500)
        walked = _walked(tmp_path, DefaultFilters(), SourceFilters(min_size=5000, max_size=100))
        assert walked == set(), "the premise of the warning"


class TestAnUnknownKindIsRefused:
    """`kinds` means "only these", so a misspelt one indexes nothing at all.
    The config accepted any string while `--kind` rejected the same value."""

    @pytest.mark.parametrize("model", ["DefaultFilters", "SourceFilters"])
    @pytest.mark.parametrize("bad", ["markdown", "nonsense", "MD"])
    def test_both_models_refuse_it(self, model: str, bad: str) -> None:
        import fnd.config as conf

        with pytest.raises(ValueError, match="no such file type"):
            getattr(conf, model)(kinds=[bad])

    @pytest.mark.parametrize(
        ("wrong", "right"),
        [
            ("markdown", "md"),
            ("Markdown", "md"),
            (".md", "md"),
            (".py", "python"),
            ("pythn", "python"),
        ],
    )
    def test_it_names_the_id_that_was_meant(self, wrong: str, right: str) -> None:
        """The usual wrong guess is the type's name or its extension, not a
        typo of the id, so plain fuzzy matching would miss the common case."""
        with pytest.raises(ValueError, match=f"did you mean '{right}'"):
            DefaultFilters(kinds=[wrong])

    @pytest.mark.parametrize("good", ["md", "pdf", "python", "javascript"])
    def test_real_ids_still_pass(self, good: str) -> None:
        assert DefaultFilters(kinds=[good]).kinds == [good]

    def test_an_unknown_kind_would_have_indexed_nothing(self, tmp_path: Path) -> None:
        """The premise: why this is refused rather than ignored."""
        (tmp_path / "a.md").write_text("hello\n")
        assert _walked(tmp_path, DefaultFilters(kinds=["md"])) == {"a.md"}
        assert _walked(tmp_path, DefaultFilters.model_construct(kinds=["markdown"])) == set()


class TestThePickerOffersWhatWouldBeIndexed:
    """The sample walked without ignore files, so a `.fndignore`-d folder
    still contributed its types and tags: the picker offered a file type the
    walk could never yield, with a count of files that are never indexed."""

    @staticmethod
    def _corpus(root: Path) -> None:
        (root / "keep.md").write_text("x\n")
        plain = root / "plain"
        plain.mkdir()
        (plain / "a.txt").write_text("y\n")
        (root / ".fndignore").write_text("plain/\n")

    def test_an_ignored_folder_contributes_nothing(self, tmp_path: Path) -> None:
        from fnd.filters.scan import sample_source
        from fnd.walk import walk

        root = tmp_path.resolve()
        self._corpus(root)
        offered = set(sample_source(root, budget_s=1.0).kinds)
        walked = {
            p.suffix.lstrip(".")
            for p in walk(roots=[root], ignore_names=(".gitignore", ".fndignore"))
        }
        assert offered == {"md"}
        assert "txt" not in offered, "a type the walk can never yield"
        assert walked == {"md"}

    def test_a_source_that_ignores_nothing_sees_everything(self, tmp_path: Path) -> None:
        """A source may switch the ignore files off; the sample must follow."""
        from fnd.filters.scan import sample_source

        root = tmp_path.resolve()
        self._corpus(root)
        assert set(sample_source(root, budget_s=1.0, ignore_names=()).kinds) == {"md", "txt"}


class TestTagsThatCannotBothHold:
    """A file must carry a required tag to pass and is dropped if it carries
    an excluded one, so when every required tag is excluded too the set is
    empty however large the corpus, and nothing said so."""

    @staticmethod
    def _survivors(root: Path, spec: Any) -> set[str]:
        from fnd.file_facts import FileFacts
        from fnd.filters.text import build_gate
        from fnd.tags import providers_for

        gate = build_gate(spec)
        providers = providers_for("linux", ["frontmatter"])
        return {
            p.name
            for p in root.iterdir()
            if p.suffix == ".md" and gate.passes(FileFacts(p, root=root, tag_providers=providers))
        }

    @staticmethod
    def _corpus(root: Path) -> None:
        (root / "keep.md").write_text("---\ntags: [keep]\n---\nx\n")
        (root / "other.md").write_text("---\ntags: [other]\n---\ny\n")

    def test_it_is_named_and_really_matches_nothing(self, tmp_path: Path) -> None:
        from fnd.filters import FilterSpec

        self._corpus(tmp_path)
        spec = FilterSpec(
            include_tags={"frontmatter": ("keep",)}, exclude_tags={"frontmatter": ("keep",)}
        )
        assert spec.impossible_bounds() == ("tags: every required tag is also excluded",)
        assert self._survivors(tmp_path, spec) == set(), "the premise of the warning"

    def test_one_of_two_required_tags_excluded_is_workable(self, tmp_path: Path) -> None:
        """Crying wolf would be worse than silence: files with the other tag
        still pass, so this must not be flagged."""
        from fnd.filters import FilterSpec

        self._corpus(tmp_path)
        spec = FilterSpec(
            include_tags={"frontmatter": ("keep", "other")},
            exclude_tags={"frontmatter": ("keep",)},
        )
        assert spec.impossible_bounds() == ()
        assert self._survivors(tmp_path, spec) == {"other.md"}

    def test_the_two_tag_sources_are_different_statements(self) -> None:
        from fnd.filters import FilterSpec

        spec = FilterSpec(include_tags={"frontmatter": ("keep",)}, exclude_tags={"os": ("keep",)})
        assert spec.impossible_bounds() == ()

    def test_an_ordinary_pair_is_not_flagged(self) -> None:
        from fnd.filters import FilterSpec

        assert FilterSpec(include_tags={"frontmatter": ("keep",)}).impossible_bounds() == ()
        assert FilterSpec(exclude_tags={"frontmatter": ("no_index",)}).impossible_bounds() == ()
