"""Excludes are an index filter: a master list every source inherits, overridden per source."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from fnd.config import (
    CollectionConfig,
    Config,
    DefaultFilters,
    Defaults,
    SourceConfig,
    SourceFilters,
    resolve_filters,
)

SYSTEM = ["**/.*", "**/.DS_Store", "**/Thumbs.db", "**/desktop.ini", "**/.git/**"]


def test_the_master_list_starts_with_system_files() -> None:
    """What the old Add collection pre-ticked is now every source's default."""
    from fnd.config import EXCLUDES_PRESETS

    assert DefaultFilters().excludes == EXCLUDES_PRESETS["hidden"]["globs"] == SYSTEM


def test_a_legacy_system_files_list_follows_the_master(tmp_path: Path) -> None:
    """The old wizard's default, in any order, is the master's: the source inherits it."""
    source = SourceConfig.model_validate({"path": tmp_path, "excludes": SYSTEM[::-1]})
    assert source.filters is None or source.filters.excludes is None
    assert source.excludes == SYSTEM


def test_a_sources_own_list_replaces_the_master() -> None:
    """Per-field override, as every filter: a source can drop an inherited exclude."""
    merged = resolve_filters(SourceFilters(excludes=["b/**"]), DefaultFilters(excludes=["a/**"]))
    assert merged.excludes == ["b/**"]


def test_an_unset_list_inherits_the_master() -> None:
    """None means inherit."""
    merged = resolve_filters(SourceFilters(), DefaultFilters(excludes=["a/**"]))
    assert merged.excludes == ["a/**"]


def test_a_source_reads_its_effective_excludes(tmp_path: Path) -> None:
    """Readers keep one name: the source's excludes are its resolved list."""
    cfg = Config(
        defaults=Defaults(filters=DefaultFilters(excludes=["a/**"])),
        collections={"notes": CollectionConfig(sources=[SourceConfig(path=tmp_path)])},
    )
    assert cfg.collections["notes"].sources[0].excludes == ["a/**"]


def test_a_legacy_source_level_list_folds_into_its_filters(tmp_path: Path) -> None:
    """A hand-written `excludes = [...]` on a source still loads, as an override."""
    source = SourceConfig.model_validate({"path": tmp_path, "excludes": ["x/**"]})
    assert source.filters is not None
    assert source.filters.excludes == ["x/**"]


def test_an_explicit_filters_list_wins_over_a_legacy_one(tmp_path: Path) -> None:
    """The current place is never overwritten by the legacy one."""
    source = SourceConfig.model_validate(
        {"path": tmp_path, "excludes": ["old/**"], "filters": {"excludes": ["new/**"]}}
    )
    assert source.filters is not None
    assert source.filters.excludes == ["new/**"]


def test_an_empty_legacy_list_inherits(tmp_path: Path) -> None:
    """An empty source list was never a choice to exclude nothing; it inherits."""
    source = SourceConfig.model_validate({"path": tmp_path, "excludes": []})
    assert source.filters is None or source.filters.excludes is None


def test_a_legacy_flat_collection_promotes_its_excludes(tmp_path: Path) -> None:
    """The old collection-level shape lands in the source's filters."""
    col = CollectionConfig.model_validate({"roots": [str(tmp_path)], "excludes": ["build/**"]})
    filters = col.sources[0].filters
    assert filters is not None
    assert filters.excludes == ["build/**"]


def _walked(cfg: Config) -> set[str]:
    from fnd.walk import walk_sources

    col = cfg.collections["notes"]
    root = col.sources[0].path.resolve()
    return {p.relative_to(root).as_posix() for p in walk_sources(sources=col.sources)}


def _tree(tmp_path: Path) -> Path:
    root = tmp_path / "notes"
    (root / "skip").mkdir(parents=True)
    (root / "keep.md").write_text("k\n", encoding="utf-8")
    (root / "skip" / "gone.md").write_text("g\n", encoding="utf-8")
    return root


def test_the_walk_obeys_the_master_list(tmp_path: Path) -> None:
    """A source that sets nothing inherits the master excludes."""
    root = _tree(tmp_path)
    cfg = Config(
        defaults=Defaults(filters=DefaultFilters(excludes=["skip/**"])),
        collections={"notes": CollectionConfig(sources=[SourceConfig(path=root)])},
    )
    assert _walked(cfg) == {"keep.md"}


def test_a_sources_empty_list_overrides_the_master(tmp_path: Path) -> None:
    """Replace semantics: a source can choose to exclude nothing."""
    root = _tree(tmp_path)
    cfg = Config(
        defaults=Defaults(filters=DefaultFilters(excludes=["skip/**"])),
        collections={
            "notes": CollectionConfig(
                sources=[SourceConfig(path=root, filters=SourceFilters(excludes=[]))]
            )
        },
    )
    assert _walked(cfg) == {"keep.md", "skip/gone.md"}


def _cfg_with(tmp_path: Path, filters: SourceFilters | None = None) -> Config:
    (tmp_path / "notes").mkdir(exist_ok=True)
    return Config(
        collections={
            "notes": CollectionConfig(
                sources=[SourceConfig(path=tmp_path / "notes", filters=filters)]
            )
        }
    )


def test_an_excludes_change_reads_as_index_filters(tmp_path: Path) -> None:
    """Excludes live in Index filters, so the reason names that."""
    from fnd.index_freshness import compare, indexed_with

    before = _cfg_with(tmp_path)
    after = _cfg_with(tmp_path, SourceFilters(excludes=["x/**"]))
    now = indexed_with(after.collections["notes"], after.defaults)
    then = indexed_with(before.collections["notes"], before.defaults)
    assert compare(now, then).reasons == ("Index filters",)


def _record_from_before_the_move(cfg: Config, legacy: list[str]) -> dict[str, Any]:
    from fnd.index_freshness import indexed_with

    old = copy.deepcopy(indexed_with(cfg.collections["notes"], cfg.defaults))
    for source in old["selection"]["sources"]:
        source["filters"].pop("excludes")
        source["excludes"] = legacy
    return old


def test_a_record_from_before_the_move_still_reads_current(tmp_path: Path) -> None:
    """Same globs, old place: current. So is the old wizard's default, in any order."""
    from fnd.index_freshness import State, compare, indexed_with

    cfg = _cfg_with(tmp_path, SourceFilters(excludes=["x/**"]))
    now = indexed_with(cfg.collections["notes"], cfg.defaults)
    assert compare(now, _record_from_before_the_move(cfg, ["x/**"])).state is State.CURRENT

    bare = _cfg_with(tmp_path)
    now_bare = indexed_with(bare.collections["notes"], bare.defaults)
    old = _record_from_before_the_move(bare, SYSTEM[::-1])
    assert compare(now_bare, old).state is State.CURRENT


def test_a_source_that_excluded_nothing_reads_outdated(tmp_path: Path) -> None:
    """It now skips desktop.ini and Thumbs.db, so saying current would be false."""
    from fnd.index_freshness import State, compare, indexed_with

    bare = _cfg_with(tmp_path)
    now = indexed_with(bare.collections["notes"], bare.defaults)
    verdict = compare(now, _record_from_before_the_move(bare, []))
    assert verdict.state is State.NEEDS_UPDATE
    assert verdict.reasons == ("Index filters",)


def test_a_legacy_flat_collection_without_excludes_inherits_the_master(tmp_path: Path) -> None:
    """No excludes in the old shape is no override: the master list still applies."""
    cfg = Config.model_validate(
        {
            "defaults": {"filters": {"excludes": ["a/**"]}},
            "collections": {"notes": {"roots": [str(tmp_path)]}},
        }
    )
    assert cfg.collections["notes"].sources[0].excludes == ["a/**"]


def test_a_folder_glob_covers_its_folder_only() -> None:
    """`skip/**` excludes everything under skip; `skip/*.md` only some of it."""
    from fnd.globs import GlobSet

    assert GlobSet.parse(["skip/**"]).covers_dir("skip")
    assert GlobSet.parse(["**/node_modules/**"]).covers_dir("a/node_modules")
    assert not GlobSet.parse(["skip/*.md"]).covers_dir("skip")
    assert not GlobSet.parse(["skip/**"]).covers_dir("skipped")


def test_an_excluded_folder_is_never_opened(tmp_path: Path, monkeypatch: Any) -> None:
    """The walk prunes it at descent instead of reading every file below."""
    import os

    import fnd.walk

    root = _tree(tmp_path)
    opened: list[str] = []
    real = os.scandir

    def spy(path: Any) -> Any:
        opened.append(Path(path).name)
        return real(path)

    monkeypatch.setattr(fnd.walk.os, "scandir", spy)
    cfg = Config(
        defaults=Defaults(filters=DefaultFilters(excludes=["skip/**"])),
        collections={"notes": CollectionConfig(sources=[SourceConfig(path=root)])},
    )
    assert _walked(cfg) == {"keep.md"}
    assert "skip" not in opened
