"""Finder-tag xattr reads and the provider registry."""

from __future__ import annotations

import plistlib
import subprocess
import sys
from pathlib import Path

import pytest

from fnd.tags import (
    TAG_PROVIDERS,
    FrontmatterTagProvider,
    MacOSFinderTagProvider,
    TagContext,
    providers_for,
    read_tags,
)

XATTR = "com.apple.metadata:_kMDItemUserTags"

darwin_only = pytest.mark.skipif(sys.platform != "darwin", reason="Finder tags are macOS-only")


def _write_tags(path: Path, tags: list[str]) -> None:
    blob = plistlib.dumps(tags).decode("utf-8")
    subprocess.run(["xattr", "-w", XATTR, blob, str(path)], check=True)


def _ctx(path: Path) -> TagContext:
    return TagContext(path=path, frontmatter=None)


@darwin_only
def test_reads_finder_tags(tmp_path: Path) -> None:
    f = tmp_path / "a.txt"
    f.write_text("x", encoding="utf-8")
    _write_tags(f, ["Work", "Red\n6"])
    assert MacOSFinderTagProvider().read(_ctx(f)) == frozenset({"work", "red"})


@darwin_only
def test_strips_colour_index_suffix(tmp_path: Path) -> None:
    """Finder stores 'Name\\n<colour>'; the colour is presentation only."""
    f = tmp_path / "a.txt"
    f.write_text("x", encoding="utf-8")
    _write_tags(f, ["Important\n1"])
    assert MacOSFinderTagProvider().read(_ctx(f)) == frozenset({"important"})


@darwin_only
def test_expands_nested_finder_tags(tmp_path: Path) -> None:
    f = tmp_path / "a.txt"
    f.write_text("x", encoding="utf-8")
    _write_tags(f, ["project/alpha"])
    assert MacOSFinderTagProvider().read(_ctx(f)) == frozenset({"project", "project/alpha"})


@darwin_only
def test_untagged_file_yields_empty(tmp_path: Path) -> None:
    f = tmp_path / "a.txt"
    f.write_text("x", encoding="utf-8")
    assert MacOSFinderTagProvider().read(_ctx(f)) == frozenset()


@darwin_only
def test_malformed_plist_yields_empty(tmp_path: Path) -> None:
    f = tmp_path / "a.txt"
    f.write_text("x", encoding="utf-8")
    subprocess.run(["xattr", "-w", XATTR, "not-a-plist", str(f)], check=True)
    assert MacOSFinderTagProvider().read(_ctx(f)) == frozenset()


def test_missing_file_yields_empty(tmp_path: Path) -> None:
    assert MacOSFinderTagProvider().read(_ctx(tmp_path / "nope.txt")) == frozenset()


def test_registry_filters_by_platform() -> None:
    assert [p.id for p in providers_for("win32", ["frontmatter", "os"])] == ["frontmatter"]
    assert [p.id for p in providers_for("linux", ["frontmatter", "os"])] == ["frontmatter"]
    assert {p.id for p in providers_for("darwin", ["frontmatter", "os"])} == {
        "frontmatter",
        "os",
    }


def test_registry_honours_the_enabled_list() -> None:
    assert [p.id for p in providers_for("darwin", ["frontmatter"])] == ["frontmatter"]
    assert [p.id for p in providers_for("darwin", [])] == []


def test_registry_ignores_unknown_source_names() -> None:
    assert [p.id for p in providers_for("darwin", ["frontmatter", "nonsense"])] == ["frontmatter"]


@darwin_only
def test_read_tags_keys_results_by_provider(tmp_path: Path) -> None:
    f = tmp_path / "a.md"
    f.write_text("x", encoding="utf-8")
    _write_tags(f, ["Red"])
    ctx = TagContext(path=f, frontmatter={"tags": ["recipe"]})
    got = read_tags(ctx, [FrontmatterTagProvider(), MacOSFinderTagProvider()])
    assert got == {"frontmatter": frozenset({"recipe"}), "os": frozenset({"red"})}


def test_read_tags_survives_a_failing_provider(tmp_path: Path) -> None:
    """One broken source must not abort the whole file's indexing."""

    class Exploding:
        id = "boom"

        def available_on(self, platform: str) -> bool:
            return True

        def read(self, ctx: TagContext) -> frozenset[str]:
            raise RuntimeError("provider blew up")

    ctx = TagContext(path=tmp_path / "a.md", frontmatter={"tags": ["ok"]})
    got = read_tags(ctx, [FrontmatterTagProvider(), Exploding()])
    assert got["frontmatter"] == frozenset({"ok"})
    assert got["boom"] == frozenset()


def test_registry_is_keyed_by_provider_id() -> None:
    assert set(TAG_PROVIDERS) == {"frontmatter", "os"}
