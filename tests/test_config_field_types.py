"""Every config value is checked at load, by its field type, whichever surface wrote it."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from fnd.config import Config, SourceConfig, load, write_setting


def _coll(**source: Any) -> dict[str, Any]:
    return {"collections": {"notes": {"sources": [{"path": "/tmp", **source}]}}}


# ── Duration (finding 3) ─────────────────────────────────────────────


@pytest.mark.parametrize("value", ["banana", "0d", "", "12x"])
def test_a_duration_that_cannot_be_used_is_refused_at_load(value: str) -> None:
    with pytest.raises(ValidationError, match="recency_half_life"):
        Config.model_validate({"ranking": {"default": {"recency_half_life": value}}})


def test_a_well_formed_duration_still_loads() -> None:
    cfg = Config.model_validate({"ranking": {"default": {"recency_half_life": " 12h "}}})
    assert cfg.ranking["default"].recency_half_life == "12h"


# ── App templates (finding 5) ────────────────────────────────────────


@pytest.mark.parametrize(
    "app",
    [
        {"argv": ["open", "{0}"]},
        {"argv": ["open", "{path"]},
        {"argv": ["open", "{path.name}"]},
        {"argv": []},
        {"argv": [""]},
        {"argv": ["open", "{page:03d}"]},
        {"url": "x://{nope}"},
        {"url": "x://{}"},
    ],
)
def test_an_app_template_that_cannot_render_is_refused_at_load(app: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        Config.model_validate(
            {"apps": {"mine": {"display_name": "Mine", "handles": ["md"], **app}}}
        )


def test_a_template_naming_known_placeholders_loads() -> None:
    cfg = Config.model_validate(
        {
            "apps": {
                "mine": {
                    "display_name": "Mine",
                    "handles": ["md"],
                    "argv": ["ed", "{path}:{line}", "{{literal}}"],
                }
            }
        }
    )
    assert cfg.apps["mine"].argv == ["ed", "{path}:{line}", "{{literal}}"]


def test_the_template_names_are_the_ones_the_renderer_supplies() -> None:
    from fnd.apps import OpenRequest, _render_vars  # pyright: ignore[reportPrivateUsage]
    from fnd.config_types import TEMPLATE_VARS

    stub = OpenRequest(path=Path("/x"), kind="md")
    assert set(_render_vars(stub)) == TEMPLATE_VARS


# ── Numeric bounds (finding 26) ──────────────────────────────────────


@pytest.mark.parametrize(
    ("key", "value", "loaded"),
    [
        ("result_limit", 0, 1),
        ("result_limit", 5000, 1000),
        ("debounce_ms", -5, 0),
        ("preview_decode_workers", 32, 16),
        ("sections_score_threshold", 1.5, 1.0),
        ("cloud_fetch_timeout_s", 7200, 3600),
    ],
)
def test_an_out_of_range_number_loads_clamped(key: str, value: float, loaded: float) -> None:
    """Earlier builds wrote any number; one that breaks nothing must not lock a user out."""
    assert getattr(Config.model_validate({"defaults": {key: value}}).defaults, key) == loaded


def test_a_negative_size_bound_and_window_clamp() -> None:
    cfg = Config.model_validate(
        {
            "defaults": {"filters": {"min_size": -1}},
            "ranking": {"default": {"proximity_max_window": 0}},
        }
    )
    assert cfg.defaults.filters.min_size == 0
    assert cfg.ranking["default"].proximity_max_window == 1


def test_the_menu_range_is_the_model_range() -> None:
    """One declaration: a row's enforced range is read from the field."""
    from fnd.config_types import field_bounds

    assert field_bounds(("defaults", "result_limit")) == (1, 1000)
    assert field_bounds(("defaults", "sections_score_threshold")) == (0.0, 1.0)
    assert field_bounds(("defaults", "fuzzy_enabled")) is None


# ── Source paths (findings 27, 28) ───────────────────────────────────


@pytest.mark.parametrize("raw", ["", "   ", "''", '""'])
def test_an_empty_source_path_is_refused(raw: str) -> None:
    with pytest.raises(ValidationError, match="path"):
        SourceConfig(path=raw)  # pyright: ignore[reportArgumentType]


@pytest.mark.parametrize("raw", ["  ~/notes  ", "'~/notes'", '"~/notes"', "~/notes\n"])
def test_a_source_path_is_cleaned_the_same_way_from_every_surface(raw: str) -> None:
    assert SourceConfig(path=raw).path == Path.home() / "notes"  # pyright: ignore[reportArgumentType]


def test_a_relative_source_path_resolves_at_load(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    assert SourceConfig(path="docs").path == tmp_path / "docs"  # pyright: ignore[reportArgumentType]


# ── Collection names (finding 29) ────────────────────────────────────


@pytest.mark.parametrize(
    "name",
    ["a/b", "..\\x", "", "zero\N{ZERO WIDTH SPACE}width", "bidi\N{RIGHT-TO-LEFT OVERRIDE}x"],
)
def test_an_unsafe_collection_name_is_refused_at_load(name: str) -> None:
    with pytest.raises(ValidationError):
        Config.model_validate({"collections": {name: {"sources": [{"path": "/tmp"}]}}})


def test_two_names_differing_only_by_case_are_refused() -> None:
    """They share one state file on a case-insensitive filesystem."""
    with pytest.raises(ValidationError, match="Notes"):
        Config.model_validate(
            {
                "collections": {
                    "Notes": {"sources": [{"path": "/tmp"}]},
                    "notes": {"sources": [{"path": "/tmp"}]},
                }
            }
        )


def test_names_differing_only_by_spacing_are_two_collections() -> None:
    cfg = Config.model_validate(
        {"collections": {n: {"sources": [{"path": "/tmp"}]} for n in ("a b", "a  b")}}
    )
    assert len(cfg.collections) == 2


def test_a_case_only_rename_is_one_write(tmp_path: Path) -> None:
    from fnd.config import rename_collection

    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        '[defaults]\ncollection = "notes"\n[collections.notes]\n'
        '[[collections.notes.sources]]\npath = "/tmp"\n',
        encoding="utf-8",
    )
    rename_collection(config_path=cfg_path, old="notes", new="Notes")
    cfg = load(cfg_path)
    assert list(cfg.collections) == ["Notes"]
    assert cfg.defaults.collection == "Notes"


def test_a_name_clash_is_found_before_the_write() -> None:
    from fnd.config_types import name_clash

    assert name_clash("NOTES", ["notes", "work"]) == "notes"
    assert name_clash("Notes", ["notes"], ignoring="notes") is None


def test_every_field_answers_setting_error() -> None:
    """A field with no Field() constraints crashed the edit bar."""
    from fnd.config_types import setting_error

    assert setting_error(("defaults", "tag_frontmatter_keys"), ["Course"]) == ""
    assert setting_error(("defaults", "fuzzy_enabled"), True) == ""
    assert setting_error(("defaults", "result_limit"), 5000) == "outside 1-1000"


@pytest.mark.parametrize("name", ["all", "Soft Eng Textbooks", "x" * 80, "Études"])
def test_a_legacy_name_that_breaks_nothing_still_loads(name: str) -> None:
    """Style rules stay write-side, so an older hand-written config keeps loading."""
    cfg = Config.model_validate({"collections": {name: {"sources": [{"path": "/tmp"}]}}})
    assert name in cfg.collections


# ── Tags (findings 30, 31) ───────────────────────────────────────────


def test_an_unknown_tag_source_is_refused() -> None:
    with pytest.raises(ValidationError, match="OS"):
        Config.model_validate({"defaults": {"filters": {"exclude_tags": {"OS": ["archive"]}}}})


def test_a_tag_that_normalises_to_nothing_is_dropped() -> None:
    cfg = Config.model_validate(
        {
            "defaults": {
                "tag_frontmatter_keys": ["", "Course"],
                "filters": {"include_tags": ["#", "x"]},
            }
        }
    )
    assert cfg.defaults.filters.include_tags == ["x"]
    assert cfg.defaults.tag_frontmatter_keys == ["Course"]


@pytest.mark.parametrize("key", [" #Course ", "COURSE", "Course"])
def test_every_reader_folds_a_frontmatter_tag_key_one_way(key: str) -> None:
    from fnd.index_freshness import indexed_with
    from fnd.tags import FrontmatterTagProvider, TagContext

    typed = Config.model_validate({"defaults": {"tag_frontmatter_keys": [key]}, **_coll()})
    plain = Config.model_validate({"defaults": {"tag_frontmatter_keys": ["course"]}, **_coll()})
    assert (
        indexed_with(typed.collections["notes"], typed.defaults)["extraction"]
        == indexed_with(plain.collections["notes"], plain.defaults)["extraction"]
    )
    ctx = TagContext(path=Path("/x.md"), frontmatter={"Course": "Algebra"})
    assert "course/algebra" in FrontmatterTagProvider(typed.defaults.tag_frontmatter_keys).read(ctx)


# ── Globs (finding 32) ───────────────────────────────────────────────


@pytest.mark.parametrize(
    ("typed", "stored"),
    [("build/", "build/**"), ("/build/**", "build/**"), ("./notes/*.md", "notes/*.md")],
)
def test_a_glob_is_stored_in_the_form_the_walk_matches(typed: str, stored: str) -> None:
    from fnd.globs import PathGlob

    source = SourceConfig(path="/tmp", includes=[typed])  # pyright: ignore[reportArgumentType]
    assert source.includes == [stored]
    assert PathGlob(stored).matches("build/out.md") or stored.startswith("notes")


@pytest.mark.parametrize("glob", ["[z-a]", "", "  ", "/", "./"])
def test_a_glob_that_cannot_match_is_refused_at_load(glob: str) -> None:
    with pytest.raises(ValidationError):
        Config.model_validate({"defaults": {"filters": {"excludes": [glob]}}})


@pytest.mark.parametrize("typed", ["/./build", "/.//a", ".//x/", "./.hidden/**", "/build/"])
def test_a_stored_glob_is_stored_again_unchanged(typed: str) -> None:
    """Otherwise every rewrite differs from the load and the save refuses."""
    once = SourceConfig(path="/tmp", includes=[typed]).includes  # pyright: ignore[reportArgumentType]
    assert SourceConfig(path="/tmp", includes=once).includes == once  # pyright: ignore[reportArgumentType]


def test_the_glob_editor_check_is_the_save_check() -> None:
    from fnd.config_types import glob_error

    assert glob_error("/")
    assert not glob_error("build/")


def test_a_glob_list_round_trips_through_its_text_form() -> None:
    from fnd.config_types import join_list, split_list

    globs = ["report,final*.md", "a\\,b", "plain/**"]
    assert split_list(join_list(globs)) == globs


# ── Setting keys (finding 34) ────────────────────────────────────────


def test_a_setting_under_a_dotted_collection_name_is_written(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        '[collections."Notes v1.2"]\n[[collections."Notes v1.2".sources]]\npath = "/tmp"\n',
        encoding="utf-8",
    )
    write_setting(
        config_path=cfg_path,
        dotted_path=("collections", "Notes v1.2", "ranking_profile"),
        value="fast",
    )
    assert load(cfg_path).collections["Notes v1.2"].ranking_profile == "fast"


def test_no_setting_path_is_formatted_from_a_name() -> None:
    """A collection or profile name may hold a dot; only a key tuple keeps it whole."""
    import ast

    root = Path(__file__).resolve().parent.parent / "fnd"
    offenders: list[str] = []
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
            if name not in ("write_setting", "write_settings"):
                continue
            for kw in node.keywords:
                keys: list[ast.expr | None] = []
                if kw.arg == "dotted_path":
                    keys = [kw.value]
                elif kw.arg == "values" and isinstance(kw.value, ast.Dict):
                    keys = list(kw.value.keys)
                elif kw.arg == "values" and isinstance(kw.value, ast.DictComp):
                    keys = [kw.value.key]
                if any(isinstance(k, ast.JoinedStr) for k in keys):
                    offenders.append(f"{path.relative_to(root)}:{node.lineno}")
    assert not offenders, offenders


def test_a_clamp_at_startup_names_the_setting(tmp_path: Path) -> None:
    from fnd.config import ensure_current

    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        "[defaults]\nresult_limit = 5000\n"
        '[[collections.notes.sources]]\npath = "/tmp"\nfilters = { min_size = -3 }\n',
        encoding="utf-8",
    )
    applied = ensure_current(cfg_path)
    assert any("defaults.result_limit from 5000 to 1000" in a for a in applied), applied
    assert any("sources[0].filters.min_size from -3 to 0" in a for a in applied), applied
    assert load(cfg_path).defaults.result_limit == 1000
