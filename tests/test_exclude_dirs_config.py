"""exclude_dirs: a per-source list of folders under the source root that are never indexed."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from fnd.config import (
    DefaultFilters,
    Defaults,
    SourceConfig,
    SourceFilters,
    load,
    resolve_filters,
)
from fnd.config_render import render_config
from fnd.filters import spec_from_resolved


def test_entries_are_normalised_deduplicated_and_folded() -> None:
    """Nested entries fold into their parent; separators and ./ are tidied."""
    own = SourceFilters(exclude_dirs=["./archive/", "archive", "archive/old", "notes//drafts"])
    assert own.exclude_dirs == ["archive", "notes/drafts"]


@pytest.mark.parametrize("bad", ["", "/etc", "a/../b", "."])
def test_a_path_that_names_no_folder_in_the_source_is_refused(bad: str) -> None:
    """The config load fails with the reason rather than excluding nothing."""
    with pytest.raises(ValueError, match=r"folder|relative|inside|itself"):
        SourceFilters(exclude_dirs=[bad])


def test_the_defaults_table_must_leave_it_empty() -> None:
    """A folder path means nothing across sources, so [defaults.filters] refuses one."""
    with pytest.raises(ValueError, match="per source"):
        Defaults(filters=DefaultFilters(exclude_dirs=["archive"]))


def test_a_source_value_resolves_over_the_empty_default() -> None:
    """Resolution carries the source's list, which the defaults model itself must accept."""
    resolved = resolve_filters(SourceFilters(exclude_dirs=["a"]), DefaultFilters())
    assert resolved.exclude_dirs == ["a"]


def test_a_source_with_folders_still_inherits_the_excludes(tmp_path: Path) -> None:
    """Excluding folders must not make the source carry its own excludes list."""
    source = SourceConfig(path=tmp_path, filters=SourceFilters(exclude_dirs=["a"]))
    assert source.exclude_dirs == ["a"]
    assert source.excludes == DefaultFilters().excludes


def test_the_spec_carries_them(tmp_path: Path) -> None:
    """The filter spec the screens edit holds the resolved folders."""
    source = SourceConfig(path=tmp_path, filters=SourceFilters(exclude_dirs=["a", "b/c"]))
    assert spec_from_resolved(source.effective_filters).exclude_dirs == ("a", "b/c")


def _written(tmp_path: Path) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(
        textwrap.dedent(
            f"""
            [[collections.notes.sources]]
            path = "{tmp_path.as_posix()}"

            [collections.notes.sources.filters]
            exclude_dirs = ["archive", "projects/old"]
            """
        ),
        encoding="utf-8",
    )
    return path


def test_a_config_file_sets_them(tmp_path: Path) -> None:
    """The documented TOML key loads onto the source."""
    source = load(_written(tmp_path)).collections["notes"].sources[0]
    assert source.exclude_dirs == ["archive", "projects/old"]


def test_they_survive_being_rendered_and_stay_off_the_defaults_table(tmp_path: Path) -> None:
    """The source table writes them; the defaults table never lists the key."""
    config = load(_written(tmp_path))
    rendered = render_config(config)
    assert 'exclude_dirs = ["archive", "projects/old"]' in rendered
    assert "exclude_dirs" not in rendered.split("[[collections.")[0]
