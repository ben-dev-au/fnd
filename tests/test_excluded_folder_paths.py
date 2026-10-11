"""Excluded folders are root-relative paths; only the top-most are stored."""

from __future__ import annotations

from pathlib import Path

import pytest

from fnd.filters import dir_tree
from fnd.filters.dir_tree import exists_exactly, fold, normalise_dir, state, toggle


@pytest.mark.parametrize(
    ("typed", "stored"),
    [
        ("archive", "archive"),
        ("archive/", "archive"),
        ("./notes//drafts", "notes/drafts"),
    ],
)
def test_a_typed_path_is_stored_in_one_shape(typed: str, stored: str) -> None:
    """Trailing and doubled separators and a leading ./ do not make a different folder."""
    assert normalise_dir(typed) == stored


def test_whitespace_in_a_name_is_kept_because_a_folder_can_have_it() -> None:
    """A folder called "notes " is not the folder called "notes"; only emptiness is refused."""
    assert normalise_dir("notes ") == "notes "
    assert normalise_dir("Smith, Jane/x y") == "Smith, Jane/x y"


@pytest.mark.parametrize("typed", ["", "   ", ".", "./", "/etc", "C:/x", "a/../b", ".."])
def test_a_path_outside_or_naming_the_source_is_refused(typed: str) -> None:
    """Empty, absolute, drive-lettered, escaping and root paths have no folder to exclude."""
    with pytest.raises(ValueError, match=r"folder|relative|inside|itself"):
        normalise_dir(typed)


def test_a_backslash_separates_folders_only_where_the_platform_says(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On POSIX a backslash is part of a name; on Windows it is a separator."""
    monkeypatch.setattr(dir_tree, "_BACKSLASH_SEPARATES", False)
    assert normalise_dir("a\\b") == "a\\b"
    monkeypatch.setattr(dir_tree, "_BACKSLASH_SEPARATES", True)
    assert normalise_dir("a\\b") == "a/b"


def test_fold_keeps_the_top_most_in_first_seen_order() -> None:
    """A folder under a listed one is already covered, and duplicates collapse."""
    assert fold(["z", "a/b", "a", "a/b/c", "z"]) == ("z", "a")


def test_state_reads_self_ancestor_and_descendant() -> None:
    """A listed folder and everything under it are on; its ancestors are partly on."""
    dirs = ("a/b",)
    assert state(dirs, "a/b") == "on"
    assert state(dirs, "a/b/c") == "on"
    assert state(dirs, "a") == "part"
    assert state(dirs, "a-b") == "off"
    assert state(dirs, "c") == "off"


def test_ticking_a_folder_replaces_what_it_covers() -> None:
    """Ticking a parent drops its children's entries; the list stays top-most."""
    assert toggle(("a/b", "z"), "a", known=(), read=()) == ("z", "a")


def test_unticking_a_listed_folder_removes_it() -> None:
    """Unticking an entry removes just that entry."""
    assert toggle(("a", "z"), "a", known=(), read=()) == ("z",)


def test_unticking_a_covered_child_splits_its_parent() -> None:
    """The siblings along the chain stay excluded; only the child comes back."""
    known = ("a", "a/b", "a/c", "a/b/x", "a/b/y")
    got = toggle(("a",), "a/b/x", known=known, read=("a", "a/b"))
    assert got is not None
    assert set(got) == {"a/c", "a/b/y"}


def test_unticking_under_an_unlisted_parent_is_refused() -> None:
    """If a parent's children were not all listed, splitting would un-exclude unseen folders."""
    known = ("a", "a/b", "a/c")
    assert toggle(("a",), "a/b", known=known, read=()) is None


def test_existence_is_checked_with_the_case_given(tmp_path: Path) -> None:
    """A path differing only in case is not found, on a case-insensitive filesystem too."""
    (tmp_path / "Archive" / "Old").mkdir(parents=True)
    assert exists_exactly(tmp_path, "Archive/Old")
    assert not exists_exactly(tmp_path, "archive/Old")
    assert not exists_exactly(tmp_path, "Archive/Missing")
    (tmp_path / "file").write_text("x", encoding="utf-8")
    assert not exists_exactly(tmp_path, "file")
