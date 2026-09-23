"""tag_sources config field and its write path."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import ValidationError

from fnd.config import Config, Defaults, write_setting


def test_defaults_to_both_sources() -> None:
    assert Defaults().tag_sources == ["frontmatter", "os"]


def test_accepts_a_single_source() -> None:
    assert Defaults(tag_sources=["frontmatter"]).tag_sources == ["frontmatter"]


def test_accepts_an_empty_list() -> None:
    """Disabling every source is legitimate: it turns the tag pane off."""
    assert Defaults(tag_sources=[]).tag_sources == []


def test_rejects_an_unknown_source() -> None:
    with pytest.raises(ValidationError, match="Input should be"):
        Defaults(tag_sources=["frontmatter", "telepathy"])  # type: ignore[list-item]


def test_round_trips_through_write_setting(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text("", encoding="utf-8")
    updated: Config = write_setting(
        config_path=cfg_path, dotted_path="defaults.tag_sources", value=["frontmatter"]
    )
    assert updated.defaults.tag_sources == ["frontmatter"]
    assert "tag_sources" in cfg_path.read_text(encoding="utf-8")


def test_the_settings_row_offers_the_known_sources_rather_than_free_text() -> None:
    """A closed set of two, typed as prose, was a spelling test."""
    from fnd.tags import TAG_PROVIDERS
    from fnd.tui.menu import KIND_PICKER, _choices_tag_sources, _provider_filters

    row = next(i for i in _provider_filters(cast("Any", None)) if i.id == "filters.tag_sources")
    assert row.kind == KIND_PICKER
    assert row.multi
    assert {c.value for c in _choices_tag_sources(cast("Any", None))} == set(TAG_PROVIDERS)


def test_enabling_a_source_needs_a_reindex(tmp_path: Path) -> None:
    """Tags are read when a file is indexed, so the row must not promise that
    turning a source on takes effect immediately."""
    import tantivy

    from fnd.index import build_index
    from fnd.tag_catalogue import tag_catalogue
    from fnd.tui.menu import _provider_filters

    src = tmp_path / "src"
    src.mkdir()
    (src / "n.md").write_text("---\ntags: [alpha]\n---\nhello\n")
    without = tmp_path / "without"
    without.mkdir()
    build_index(roots=[src], index_dir=without, collection="default", tag_sources=("os",))
    catalogue = tag_catalogue(tantivy.Index.open(str(without)), collections=["default"])
    assert catalogue["frontmatter"] == [], "a source off at index time stores no tags"

    row = next(i for i in _provider_filters(cast("Any", None)) if i.id == "filters.tag_sources")
    assert "no reindex" not in row.description


def test_the_config_says_a_folder_tag_is_not_inherited() -> None:
    """Tagging a folder is the natural gesture for "keep this out", and the
    tag is read per file, so a folder's own tag applies to nothing."""
    from fnd.config import DefaultFilters

    description = DefaultFilters.model_fields["exclude_tags"].description or ""
    assert "folder" in description.lower(), description


def test_the_tag_sources_row_says_it_too() -> None:
    from fnd.tui.menu import _provider_filters

    row = next(i for i in _provider_filters(cast("Any", None)) if i.id == "filters.tag_sources")
    assert "per file" in row.description
    assert "folder" in row.description


@pytest.mark.skipif(sys.platform != "darwin", reason="Finder tags are macOS-only")
def test_a_folder_tag_really_does_not_reach_its_files(tmp_path: Path) -> None:
    """The premise, measured rather than assumed."""
    import plistlib
    import subprocess

    from fnd.tags import TAG_PROVIDERS, TagContext, read_tags

    folder = tmp_path / "secret"
    folder.mkdir()
    (folder / "a.md").write_text("x\n")
    payload = plistlib.dumps(["no_index"], fmt=plistlib.FMT_BINARY).hex()
    subprocess.run(
        ["xattr", "-wx", "com.apple.metadata:_kMDItemUserTags", payload, str(folder)],
        check=False,
    )
    providers = [p for p in TAG_PROVIDERS.values() if p.available_on(sys.platform)]
    on_folder = read_tags(TagContext(path=folder, frontmatter=None), providers)
    on_file = read_tags(TagContext(path=folder / "a.md", frontmatter=None), providers)
    assert "no_index" in on_folder["os"], "the tag was not written; test setup failed"
    assert not on_file["os"], "documented behaviour: the file does not inherit it"
