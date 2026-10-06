"""Config v3 moves each source's excludes into its index filters."""

from __future__ import annotations

from typing import Any

from fnd.config_migrations import CONFIG_VERSION, migrate


def _v2(**source: Any) -> dict[str, Any]:
    return {"config_version": 2, "collections": {"notes": {"sources": [{"path": "~/n", **source}]}}}


def _source(raw: dict[str, Any]) -> dict[str, Any]:
    return raw["collections"]["notes"]["sources"][0]


def test_a_source_list_moves_into_its_filters() -> None:
    """The same globs, in the place every other filter lives."""
    raw = _v2(excludes=["build/**"])
    migrate(raw)
    assert "excludes" not in _source(raw)
    assert _source(raw)["filters"]["excludes"] == ["build/**"]


def test_an_empty_list_is_dropped_so_the_source_inherits() -> None:
    """An empty list was never a choice to exclude nothing; it inherits."""
    raw = _v2(excludes=[])
    migrate(raw)
    assert "excludes" not in _source(raw)
    assert "excludes" not in _source(raw).get("filters", {})


def test_the_old_wizard_default_is_dropped_so_the_source_follows_the_master() -> None:
    """The System files globs, in any order, are the master's default now."""
    raw = _v2(excludes=["**/.git/**", "**/desktop.ini", "**/Thumbs.db", "**/.DS_Store", "**/.*"])
    migrate(raw)
    assert "excludes" not in _source(raw)
    assert "excludes" not in _source(raw).get("filters", {})


def test_an_existing_filters_list_is_kept() -> None:
    """The current place is never overwritten by the legacy one."""
    raw = _v2(excludes=["old/**"], filters={"excludes": ["new/**"]})
    migrate(raw)
    assert _source(raw)["filters"]["excludes"] == ["new/**"]
    assert "excludes" not in _source(raw)


def test_the_step_is_reported() -> None:
    """The user is told what the upgrade did."""
    version, applied = migrate(_v2(excludes=["a/**"]))
    assert version == CONFIG_VERSION == 3
    assert any("excludes" in note for note in applied)


def test_a_released_config_with_the_old_wizard_default_follows_the_master() -> None:
    """No version means v2 respells the globs first; the respelled default still inherits."""
    wizard = ["**/.*", "**/.DS_Store", "**/Thumbs.db", "**/desktop.ini", "**/.git/**"]
    raw: dict[str, Any] = {
        "collections": {"notes": {"sources": [{"path": "~/n", "excludes": wizard}]}}
    }
    migrate(raw)
    assert "excludes" not in _source(raw)
    assert "excludes" not in _source(raw).get("filters", {})


def test_a_single_string_is_one_glob() -> None:
    """A hand-written string is one glob, never one per character."""
    raw = _v2(excludes="**/node_modules/**")
    migrate(raw)
    assert _source(raw)["filters"]["excludes"] == ["**/node_modules/**"]
