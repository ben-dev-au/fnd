"""The canonical config: generated from the models, and what that guarantees."""

from __future__ import annotations

import datetime as dt
import inspect
import os
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from fnd import config as conf
from fnd.config_migrations import CONFIG_VERSION, ConfigTooNewError, check_version, migrate
from fnd.config_render import (
    _SOURCE_ORDER,
    DEFAULT_GROUPS,
    PRESERVE_BEGIN,
    PRESERVE_END,
    _set_fields,
    extract_preserved,
    key,
    path_value,
    render_config,
)


def _models() -> list[tuple[str, type[BaseModel]]]:
    return [
        (n, v)
        for n, v in vars(conf).items()
        if inspect.isclass(v)
        and issubclass(v, BaseModel)
        and v is not BaseModel
        and n != "_ConfigModel"
    ]


def _sample() -> conf.Config:
    return conf.Config(
        # Set explicitly rather than inherited: the autouse conftest rewrites
        # these two defaults, so a fixture reading them proves less.
        # `result_limit` must differ from the model default or it renders
        # commented out, which is the opposite of what two tests below assert.
        defaults=conf.Defaults(
            result_limit=137,
            fuzzy_enabled=False,
            preview_load_debounce_ms=150,
            preview_prefetch_count=4,
        ),
        collections={
            "notes": conf.CollectionConfig(
                sources=[
                    conf.SourceConfig(
                        path=Path("~/Notes"),
                        excludes=["**/.git/**"],
                        app="obsidian",
                        app_params={"vault": "Main"},
                        filters=conf.SourceFilters(kinds=["md"], max_size=50_000_000),
                    )
                ]
            ),
            "Soft Eng Books": conf.CollectionConfig(
                sources=[conf.SourceConfig(path=Path("~/Books"))]
            ),
        },
    )


def _reload(text: str) -> conf.Config:
    """Parse rendered output the way `load` does: `config_version` is consumed
    by the version check, and Config forbids anything it does not know."""
    raw = tomllib.loads(text)
    check_version(raw)
    return conf.Config.model_validate(raw)


def _maximal() -> conf.Config:
    """Every non-deprecated field set to something other than its default.

    A sample that sets only the fields the renderer happens to handle proves
    nothing; this one fails the moment a field is added and not rendered.
    """
    filters: dict[str, Any] = {
        "respect_gitignore": False,
        "respect_fndignore": False,
        "include_tags": ["keep"],
        "exclude_tags": ["drop"],
        "kinds": ["md"],
        "min_size": 1,
        "max_size": 50_000_000,
        "created_after": dt.date(2020, 1, 1),
        "created_before": dt.date(2030, 1, 1),
        "modified_after": dt.date(2021, 1, 1),
        "modified_before": dt.date(2031, 1, 1),
        "frontmatter": "Course == 'X'",
        "expression": "file.size > 1",
    }
    return conf.Config(
        defaults=conf.Defaults(
            collection="notes",
            tag_sources=["frontmatter"],
            tag_frontmatter_keys=["Course"],
            result_limit=11,
            preview_chunks=6,
            debounce_ms=201,
            drill_summary_mode="smart",
            sections_score_threshold=0.6,
            sections_per_file_max=201,
            preview_decode_workers=5,
            preview_warm_margin=3,
            preview_load_debounce_ms=151,
            preview_prefetch_count=5,
            fuzzy_enabled=False,
            fuzzy_min_term_chars=4,
            indexer_auto_resume=True,
            cache_at_index_time=False,
            cloud_fetch_timeout_s=61,
            scrollbar_match_highlight=True,
            multicolour_highlights=False,
            preview_scroll_animation=False,
            render_mermaid=False,
            skip_junk_dirs=False,
            extra_junk_dirs=["junk"],
            filters=conf.DefaultFilters(**filters),
        ),
        collections={
            "notes": conf.CollectionConfig(
                sources=[
                    conf.SourceConfig(
                        path=Path("~/Notes"),
                        includes=["**/*.md"],
                        excludes=["**/.git/**"],
                        follow_symlinks=True,
                        # `clears` exists only on a source, so the shared
                        # `filters` dict cannot carry it.
                        filters=conf.SourceFilters(**filters, clears=["min_size"]),
                        app="obsidian",
                        app_for={"md": "obsidian"},
                        app_params={"vault": "Main"},
                    )
                ],
                includes=["**/*.md"],
                excludes=["**/tmp/**"],
                follow_symlinks=True,
                ranking_profile="tuned",
            ),
            "Soft Eng Books": conf.CollectionConfig(sources=[]),
        },
        ranking={
            "tuned": conf.RankingProfileConfig(
                recency_boost=0.3,
                recency_half_life="90d",
                filetype_boosts={"md": 1.2},
                phrase_proximity=0.4,
                proximity_max_window=40,
                bm25_k1=1.3,
                bm25_b=0.8,
            )
        },
        apps={
            "obsidian": conf.AppConfig(
                display_name="Obsidian",
                handles=["md"],
                argv=["open", "{path}"],
                notes="a note",
            )
        },
        app_defaults={"md": "obsidian"},
    )


class TestTheWholeSurfaceRoundTrips:
    """Every collection-level field round-trips; a sample that sets only what
    the renderer handles cannot fail."""

    def test_the_fixture_really_sets_every_field(self) -> None:
        config = _maximal()
        unset: list[str] = []

        def check(model: BaseModel, label: str, skip: frozenset[str] = frozenset()) -> None:
            for name, info in type(model).model_fields.items():
                # A required field is always set; only an optional one can
                # silently sit at its default and never exercise the renderer.
                if info.deprecated or name in skip or info.is_required():
                    continue
                if getattr(model, name) == info.get_default(call_default_factory=True):
                    unset.append(f"{label}.{name}")

        check(config, "Config")
        check(config.defaults, "Defaults")
        check(config.defaults.filters, "DefaultFilters")
        source = config.collections["notes"].sources[0]
        check(source, "SourceConfig")
        assert source.filters is not None
        check(source.filters, "SourceFilters")
        # roots is the legacy flat shape and cannot coexist with sources.
        check(config.collections["notes"], "CollectionConfig", skip=frozenset({"roots"}))
        check(config.ranking["tuned"], "RankingProfileConfig")
        # argv and url are mutually exclusive, so one must stay unset.
        check(config.apps["obsidian"], "AppConfig", skip=frozenset({"url"}))
        assert not unset, f"fixture leaves fields at their default: {unset}"

    def test_it_survives_a_render(self) -> None:
        config = _maximal()
        assert _reload(render_config(config)) == config

    def test_an_empty_collection_is_not_lost(self) -> None:
        back = _reload(render_config(_maximal()))
        assert "Soft Eng Books" in back.collections
        assert back.collections["Soft Eng Books"].sources == []


class TestDocumentationCannotDrift:
    """The gates that make "every key arrives explained" true, not aspirational."""

    def test_every_field_carries_a_description(self) -> None:
        """The exemption is SourceFilters alone, which mirrors DefaultFilters
        field for field. Keying it on the field name exempted that name in
        every model, and exempted DefaultFilters from itself."""
        missing: list[str] = []
        for name, model in _models():
            for field, info in model.model_fields.items():
                if info.description:
                    continue
                if model is conf.SourceFilters and field in conf.DefaultFilters.model_fields:
                    continue
                missing.append(f"{name}.{field}")
        assert not missing, f"undocumented fields reach a user's config: {missing}"

    def test_no_description_is_a_paragraph(self) -> None:
        """A config file wants a line or two per key, not source-comment
        rationale. Long prose belongs in the docs, not in every user's file."""
        long = {
            f"{name}.{field}": len(info.description.split())
            for name, model in _models()
            for field, info in model.model_fields.items()
            if info.description and len(info.description.split()) > 25
        }
        assert not long, f"descriptions too long for a config file: {long}"

    def test_no_generated_prose_uses_an_em_dash(self) -> None:
        """They read as machine-written and the config is user-facing."""
        assert "\u2014" not in conf.starter_config()

    def test_source_filters_borrows_the_defaults_prose(self) -> None:
        """One vocabulary, described once: writing it twice is the duplication
        the renderer exists to remove."""
        rendered = render_config(_sample())
        assert conf.DefaultFilters.model_fields["kinds"].description is not None
        assert "Restrict to these file types" in rendered

    def test_every_default_is_placed_in_exactly_one_group(self) -> None:
        """A field added to Defaults and not grouped would render nowhere."""
        placed = [n for _title, names in DEFAULT_GROUPS for n in names]
        # `filters` renders as its own table, not a key in [defaults].
        assert sorted(placed) == sorted(set(conf.Defaults.model_fields) - {"filters"})
        assert len(placed) == len(set(placed))


class TestRoundTrip:
    def test_a_config_survives_being_rendered(self) -> None:
        config = _sample()
        back = _reload(render_config(config))
        assert back == config

    def test_rendering_is_idempotent(self) -> None:
        first = render_config(_sample())
        again = _reload(first)
        assert render_config(again) == first

    def test_an_unset_field_renders_as_its_own_example(self) -> None:
        rendered = render_config(_sample())
        assert "# preview_chunks = 5" in rendered

    def test_a_set_field_renders_live(self) -> None:
        assert "\nresult_limit = 137" in render_config(_sample())

    def test_large_numbers_get_digit_separators(self) -> None:
        assert "max_size = 50_000_000" in render_config(_sample())


class TestDeterminism:
    """`ensure_current` compares its render against the file and rewrites, with
    a backup, on any difference. Anything hash-ordered means that fires on
    every launch."""

    def test_fields_render_in_declaration_order(self) -> None:
        """Eleven fields, so hash order cannot coincide with declaration order.
        With four it could, roughly one seed in twenty-four."""
        spec = conf.SourceFilters(
            respect_gitignore=False,
            respect_fndignore=False,
            include_tags=["keep"],
            exclude_tags=["drop"],
            kinds=["md"],
            min_size=1,
            max_size=5,
            created_after=dt.date(2020, 1, 1),
            modified_after=dt.date(2021, 1, 1),
            frontmatter="a == 'b'",
            expression="file.size > 1",
        )
        rendered = [line.split(" =")[0] for line in _set_fields(spec)]
        assert len(rendered) >= 10
        assert rendered == [n for n in conf.SourceFilters.model_fields if n in rendered]

    def test_a_source_orders_named_fields_first_then_declaration_order(self) -> None:
        """`app_for` and `app_params` sit outside _SOURCE_ORDER, so they take
        the branch where hash ordering would show; a source using only ordered
        fields never reaches it."""
        source = conf.SourceConfig(
            path=Path("~/N"),
            includes=["**/*.md"],
            follow_symlinks=True,
            app_for={"md": "obsidian"},
            app_params={"vault": "Main"},
        )
        rendered = [line.split(" =")[0] for line in _set_fields(source, _SOURCE_ORDER)]
        assert rendered[0] == "path"
        assert rendered[-2:] == ["app_for", "app_params"]


class TestPortability:
    def test_a_home_path_is_written_back_as_a_tilde(self) -> None:
        """The model expands ~ on load; without re-tilding every generated
        config bakes in one machine's absolute paths."""
        assert path_value(Path.home() / "Notes") == '"~/Notes"'
        assert '"~/Notes"' in render_config(_sample())

    def test_a_path_outside_home_is_left_absolute(self) -> None:
        assert path_value(Path("/opt/corpus")) == '"/opt/corpus"'

    def test_a_collection_name_needing_quotes_gets_them(self) -> None:
        assert key("Soft Eng Books") == '"Soft Eng Books"'
        assert key("notes") == "notes"
        tomllib.loads(render_config(_sample()))  # would raise if unquoted


class TestValueShapes:
    """Every writer replaces the whole file, so a value the renderer cannot
    express is dropped or corrupts it. `_maximal` gates field *names*; these
    gate the shapes those fields can hold, which is where the losses were."""

    def _round_trip(self, config: conf.Config) -> conf.Config:
        return _reload(render_config(config))

    def _with_notes(self, notes: str) -> conf.Config:
        return conf.Config(
            apps={"x": conf.AppConfig(display_name="X", handles=["md"], argv=["open"], notes=notes)}
        )

    def test_an_explicit_empty_list_survives(self) -> None:
        """`None` inherits, `[]` overrides to nothing. Dropping `[]` made
        unticking an inherited tag for one source a silent no-op."""
        config = conf.Config(
            collections={
                "n": conf.CollectionConfig(
                    sources=[
                        conf.SourceConfig(
                            path=Path("~/N"), filters=conf.SourceFilters(exclude_tags=[])
                        )
                    ]
                )
            }
        )
        source = self._round_trip(config).collections["n"].sources[0]
        assert source.filters is not None
        assert source.filters.exclude_tags == []

    @pytest.mark.parametrize(
        "notes", ["one\ntwo", "a\tb", 'quote" here', "back\\slash", "bell\x07"]
    )
    def test_an_awkward_string_survives(self, notes: str) -> None:
        """A raw newline ended the string early and left the file unparseable."""
        assert self._round_trip(self._with_notes(notes)).apps["x"].notes == notes

    def test_an_inline_table_key_needing_quotes_survives(self) -> None:
        """`key` quotes a table header; the inline-table branch did not use it."""
        config = conf.Config(
            collections={
                "n": conf.CollectionConfig(
                    sources=[conf.SourceConfig(path=Path("~/N"), app_params={"my key": "v"})]
                )
            }
        )
        source = self._round_trip(config).collections["n"].sources[0]
        assert source.app_params == {"my key": "v"}


class TestPreservedBlock:
    def test_the_notes_block_survives_a_write(self, tmp_path: Path) -> None:
        path = tmp_path / "config.toml"
        path.write_text(render_config(_sample(), preserved="# my note"), encoding="utf-8")
        conf.write_setting(config_path=path, dotted_path="defaults.result_limit", value=25)
        assert "# my note" in path.read_text(encoding="utf-8")

    def test_text_outside_the_block_does_not_survive(self, tmp_path: Path) -> None:
        """The trade the canonical form makes, stated as a test."""
        path = tmp_path / "config.toml"
        path.write_text(render_config(_sample()) + "\n# stray note\n", encoding="utf-8")
        conf.write_setting(config_path=path, dotted_path="defaults.result_limit", value=25)
        assert "# stray note" not in path.read_text(encoding="utf-8")

    def test_live_toml_in_the_block_is_not_echoed_back(self) -> None:
        """It is real config: it parses into the model and renders in its own
        table, so keeping the text here declared that table twice and the file
        stopped loading."""
        text = f"{PRESERVE_BEGIN}\n# mine\n[defaults]\nresult_limit = 9\n{PRESERVE_END}"
        assert extract_preserved(text) == "# mine"

    def test_a_missing_end_marker_stops_at_the_generated_prose(self) -> None:
        """Running to end of file swallowed fnd's own comments, which were then
        preserved forever: 206 lines became 392 and grew on every write."""
        text = f"{PRESERVE_BEGIN}\n# mine\n\n# ── Defaults ──\n\nresult_limit = 5\n# after\n"
        assert extract_preserved(text) == "# mine\n\n# ── Defaults ──"

    def test_a_missing_end_marker_keeps_the_block(self) -> None:
        """Returning nothing deleted whatever the user had written."""
        assert extract_preserved(f"{PRESERVE_BEGIN}\n# kept\n") == "# kept"

    def test_a_blank_line_inside_the_block_survives(self) -> None:
        text = f"{PRESERVE_BEGIN}\n# one\n\n# two\n{PRESERVE_END}"
        assert extract_preserved(text) == "# one\n\n# two"

    def test_extract_ignores_a_file_without_markers(self) -> None:
        assert extract_preserved("# nothing here\nresult_limit = 5\n") == ""

    def test_the_marker_is_present_even_when_empty(self) -> None:
        assert PRESERVE_BEGIN in render_config(_sample())


class TestRefusingBadOutput:
    def test_a_render_that_does_not_round_trip_is_refused(self, tmp_path: Path) -> None:
        """Every writer replaces the whole file, so publishing output that
        reads back differently would lose the difference silently."""
        import fnd.config_render as module

        path = tmp_path / "config.toml"
        path.write_text(render_config(_sample()), encoding="utf-8")
        before = path.read_text(encoding="utf-8")
        original = module.render_config

        def lossy(config: object, **kw: object) -> str:
            return original(config, **kw).replace("result_limit = 137", "")  # type: ignore[arg-type]

        module.render_config = lossy
        try:
            with pytest.raises(ValueError, match="round-trip"):
                conf.write_setting(config_path=path, dotted_path="defaults.debounce_ms", value=99)
        finally:
            module.render_config = original
        assert path.read_text(encoding="utf-8") == before, "the old config was not kept"


class TestSlashlessGlobsKeepTheirReach:
    """A slashless glob keeps its reach through migration: `*` stops at a
    separator, so `includes = ["*.md"]` written for the whole tree would narrow
    such a source to its root and let the next update prune everything below
    it out of the index."""

    def test_a_slashless_glob_is_anchored_on_migration(self, tmp_path: Path) -> None:
        path = tmp_path / "config.toml"
        path.write_text(
            '[[collections.n.sources]]\npath = "~/N"\n'
            'includes = ["*.md", "*.txt"]\nexcludes = ["drafts/**", "*.tmp"]\n',
            encoding="utf-8",
        )
        conf.ensure_current(path)
        source = conf.load(path).collections["n"].sources[0]
        assert source.includes == ["**/*.md", "**/*.txt"]
        assert source.excludes == ["drafts/**", "**/*.tmp"], "drafts/** already says it"

    def test_the_anchored_glob_still_reaches_a_nested_file(self, tmp_path: Path) -> None:
        """The point of the migration, not just its text."""
        from fnd.walk import walk

        (tmp_path / "sub").mkdir()
        for rel in ("a.md", "sub/b.md"):
            (tmp_path / rel).write_text("x", encoding="utf-8")
        root = tmp_path.resolve()
        got = {p.relative_to(root).as_posix() for p in walk(roots=[root], includes=["**/*.md"])}
        assert got == {"a.md", "sub/b.md"}


class TestUnknownKeysFailLoudly:
    """The renderer only knows the schema and every writer replaces the whole
    file, so a key the models ignore is deleted on the next save. Refusing at
    load is the only place that can name it."""

    @pytest.mark.parametrize(
        ("case", "text", "named"),
        [
            ("top-level table", "[mystery]\nk = 1\n", "mystery"),
            ("defaults typo", "[defaults]\nresult_limitt = 5\n", "result_limitt"),
            ("filter field", "[defaults.filters]\nnope = 1\n", "nope"),
            (
                "source key",
                '[[collections.n.sources]]\npath = "~/N"\nmystery = 1\n',
                "mystery",
            ),
        ],
    )
    def test_an_unknown_key_is_refused_and_named(
        self, case: str, text: str, named: str, tmp_path: Path
    ) -> None:
        path = tmp_path / "config.toml"
        path.write_text(text, encoding="utf-8")
        with pytest.raises(Exception, match=named):
            conf.load(path)

    def test_the_starter_config_still_loads(self, tmp_path: Path) -> None:
        """Forbidding extras would be worthless if our own output tripped it."""
        path = tmp_path / "config.toml"
        path.write_text(conf.starter_config(), encoding="utf-8")
        conf.load(path)


class TestABadConfigIsLegible:
    """Refusing an unknown key is only an improvement if the refusal reads."""

    def test_the_cli_names_the_key_instead_of_dumping_a_validation_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import subprocess
        import sys

        home = tmp_path / "data"
        (home / "fnd").mkdir(parents=True)
        (home / "fnd" / "config.toml").write_text(
            "[defaults]\nresult_limitt = 5\n", encoding="utf-8"
        )
        result = subprocess.run(
            [sys.executable, "-m", "fnd", "search", "x"],
            capture_output=True,
            text=True,
            env={**os.environ, "XDG_DATA_HOME": str(home), "PYTHONPATH": str(Path.cwd())},
            timeout=120,
        )
        assert result.returncode == 1
        assert "could not be loaded" in result.stderr
        assert "defaults.result_limitt" in result.stderr
        assert "Traceback" not in result.stderr


class TestMigration:
    def test_the_starter_carries_the_current_version(self) -> None:
        """Hardcoding 1 would make a later migration re-run on a fresh file."""
        assert tomllib.loads(conf.starter_config())["config_version"] == CONFIG_VERSION

    def test_a_fresh_config_is_stamped_with_the_current_version(self) -> None:
        raw: dict[str, object] = {}
        version, applied = migrate(raw)
        assert version == CONFIG_VERSION
        assert applied

    def test_a_current_config_needs_nothing(self) -> None:
        assert migrate({"config_version": CONFIG_VERSION}) == (CONFIG_VERSION, [])

    def test_a_newer_config_is_refused_rather_than_silently_stripped(self) -> None:
        with pytest.raises(ConfigTooNewError):
            migrate({"config_version": CONFIG_VERSION + 1})

    def test_the_version_key_never_reaches_the_model(self) -> None:
        """Config forbids unknown keys, so migrate must consume it."""
        raw: dict[str, object] = {"config_version": CONFIG_VERSION}
        migrate(raw)
        assert "config_version" not in raw

    def test_ensure_current_rewrites_a_legacy_file_and_keeps_a_backup(self, tmp_path: Path) -> None:
        path = tmp_path / "config.toml"
        path.write_text(
            '[[collections.n.sources]]\npath = "~/Notes"\nfrontmatter_filter = "Course == \'X\'"\n',
            encoding="utf-8",
        )
        applied = conf.ensure_current(path)
        assert applied
        text = path.read_text(encoding="utf-8")
        assert f"config_version = {CONFIG_VERSION}" in text
        assert "\nfrontmatter_filter" not in text, "legacy key kept instead of retired"
        assert "filters]" in text, "the rule should have moved into the filters table"
        assert list(tmp_path.glob("config.toml.bak-*")), "no backup written"

    def test_a_legacy_rule_moves_rather_than_vanishing(self, tmp_path: Path) -> None:
        """The renderer does not advertise a deprecated key, so without the
        transform that moves it the rule is silently deleted on first write."""
        path = tmp_path / "config.toml"
        path.write_text(
            '[[collections.n.sources]]\npath = "~/Notes"\nfrontmatter_filter = "Course == \'X\'"\n',
            encoding="utf-8",
        )
        conf.ensure_current(path)
        source = conf.load(path).collections["n"].sources[0]
        assert source.filters is not None
        assert source.filters.frontmatter == "Course == 'X'"

    def test_a_deprecated_field_still_set_is_written_not_dropped(self) -> None:
        """Fail-safe: a missing migration must not delete a user's rule."""
        config = conf.Config(
            collections={
                "n": conf.CollectionConfig(
                    sources=[
                        conf.SourceConfig(path=Path("~/N"), frontmatter_filter="Course == 'X'")
                    ]
                )
            }
        )
        assert "frontmatter_filter" in render_config(config)

    def test_deleting_the_last_collection_is_allowed(self, tmp_path: Path) -> None:
        """An emptied table renders as nothing, which the drop guard read as
        losing it. Deleting your only collection then failed with no handler."""
        path = tmp_path / "config.toml"
        path.write_text('[[collections.only.sources]]\npath = "~/N"\n', encoding="utf-8")
        conf.ensure_current(path)
        conf.delete_collection(config_path=path, name="only")
        assert conf.load(path).collections == {}

    def test_ensure_current_is_a_no_op_on_a_current_file(self, tmp_path: Path) -> None:
        path = tmp_path / "config.toml"
        path.write_text(render_config(_sample()), encoding="utf-8")
        assert conf.ensure_current(path) == []
        assert not list(tmp_path.glob("config.toml.bak-*"))


class TestWritersStayCanonical:
    @pytest.mark.parametrize(
        "write",
        [
            lambda p: conf.write_setting(
                config_path=p, dotted_path="defaults.result_limit", value=7
            ),
            lambda p: conf.write_settings(config_path=p, values={"defaults.debounce_ms": 90}),
            lambda p: conf.write_collection(
                config_path=p,
                name="extra",
                collection=conf.CollectionConfig(sources=[conf.SourceConfig(path=Path("~/E"))]),
            ),
            lambda p: conf.write_collection_source(
                config_path=p,
                collection_name="notes",
                source=conf.SourceConfig(path=Path("~/More")),
            ),
            lambda p: conf.delete_collection(config_path=p, name="notes"),
        ],
    )
    def test_every_writer_leaves_the_file_in_canonical_form(
        self, write: Callable[[Path], object], tmp_path: Path
    ) -> None:
        path = tmp_path / "config.toml"
        path.write_text(render_config(_sample(), preserved="# kept"), encoding="utf-8")
        write(path)
        text = path.read_text(encoding="utf-8")
        reloaded = conf.load(path)
        assert render_config(reloaded, preserved="# kept") == text
        assert "# kept" in text

    def test_clearing_a_setting_removes_the_key(self, tmp_path: Path) -> None:
        path = tmp_path / "config.toml"
        path.write_text(render_config(_sample()), encoding="utf-8")
        conf.write_setting(config_path=path, dotted_path="defaults.result_limit", value=None)
        assert conf.load(path).defaults.result_limit == conf.Defaults().result_limit
