"""A folder in exclude_dirs is skipped unopened, by exact path under the source root."""

from __future__ import annotations

from pathlib import Path

from fnd.config import SourceConfig, SourceFilters
from fnd.walk import walk, walk_sources


def _touch(p: Path) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("x", encoding="utf-8")


def _names(root: Path, **kw: object) -> set[str]:
    return {p.relative_to(root).as_posix() for p in walk(roots=[root], **kw)}  # type: ignore[arg-type]


def test_a_folder_and_everything_under_it_is_skipped(tmp_path: Path) -> None:
    """Nested files go with their folder; siblings stay."""
    _touch(tmp_path / "keep.md")
    _touch(tmp_path / "archive" / "a.md")
    _touch(tmp_path / "archive" / "deep" / "b.md")
    _touch(tmp_path / "other" / "c.md")
    assert _names(tmp_path, exclude_dirs=["archive"]) == {"keep.md", "other/c.md"}


def test_only_that_path_is_excluded_not_a_same_named_folder_elsewhere(tmp_path: Path) -> None:
    """The entry is root-relative: `old` leaves the top-level folder, not `notes/old`."""
    _touch(tmp_path / "old" / "a.md")
    _touch(tmp_path / "notes" / "old" / "b.md")
    assert _names(tmp_path, exclude_dirs=["old"]) == {"notes/old/b.md"}


def test_a_nested_path_excludes_just_that_folder(tmp_path: Path) -> None:
    """`notes/drafts` leaves `notes/final` indexed."""
    _touch(tmp_path / "notes" / "drafts" / "a.md")
    _touch(tmp_path / "notes" / "final" / "b.md")
    assert _names(tmp_path, exclude_dirs=["notes/drafts"]) == {"notes/final/b.md"}


def test_glob_characters_in_a_folder_name_are_literal(tmp_path: Path) -> None:
    """A folder called `[draft]` excludes itself, and `a[bc]` does not act as a pattern."""
    _touch(tmp_path / "[draft]" / "a.md")
    _touch(tmp_path / "d" / "b.md")
    _touch(tmp_path / "ab" / "c.md")
    _touch(tmp_path / "a[bc]" / "e.md")
    got = _names(tmp_path, exclude_dirs=["[draft]", "a[bc]"])
    assert got == {"d/b.md", "ab/c.md"}


def test_an_include_glob_cannot_readmit_an_excluded_folder(tmp_path: Path) -> None:
    """Exclusion wins over includes, as it does for excludes."""
    _touch(tmp_path / "archive" / "a.md")
    _touch(tmp_path / "keep.md")
    assert _names(tmp_path, includes=["**/*.md"], exclude_dirs=["archive"]) == {"keep.md"}


def test_a_source_walks_with_its_own_folders(tmp_path: Path) -> None:
    """walk_sources reads the source's resolved exclude_dirs."""
    _touch(tmp_path / "archive" / "a.md")
    _touch(tmp_path / "keep.md")
    source = SourceConfig(path=tmp_path, filters=SourceFilters(exclude_dirs=["archive"]))
    assert {p.name for p in walk_sources(sources=[source])} == {"keep.md"}
