"""The folder picker lists what the walk could enter, bounded in count and time."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from fnd.config import DEFAULT_JUNK_DIRS
from fnd.walk import list_dirs


def _tree(root: Path, *names: str) -> None:
    for name in names:
        (root / name).mkdir(parents=True)


def test_it_lists_nested_folders_breadth_first_and_marks_them_read(tmp_path: Path) -> None:
    """Every folder is listed relative to the root, with its parents marked fully read."""
    _tree(tmp_path, "b", "a/x", "a/y")
    got = list_dirs(tmp_path, skip_dirs=frozenset())
    assert got.dirs == ("a", "b", "a/x", "a/y")
    assert got.read >= {"", "a", "b"}
    assert not got.truncated


def test_it_skips_what_the_walk_never_enters(tmp_path: Path) -> None:
    """Hidden, junk and glob-excluded folders are not offered."""
    _tree(tmp_path, ".git", "node_modules", "scratch", "keep")
    got = list_dirs(tmp_path, skip_dirs=DEFAULT_JUNK_DIRS, excludes=["scratch/**"])
    assert got.dirs == ("keep",)


def test_a_cap_marks_the_listing_truncated_and_the_cut_folder_unread(tmp_path: Path) -> None:
    """Past max_dirs the parent is not marked read, so a split under it is refused."""
    _tree(tmp_path, *[f"d{i}" for i in range(5)])
    got = list_dirs(tmp_path, skip_dirs=frozenset(), max_dirs=3)
    assert len(got.dirs) == 3
    assert got.truncated
    assert "" not in got.read


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
def test_a_symlinked_folder_is_not_listed_unless_followed(tmp_path: Path) -> None:
    """Links are followed only when the source follows them."""
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "link").symlink_to(real, target_is_directory=True)
    assert list_dirs(tmp_path, skip_dirs=frozenset()).dirs == ("real",)
    followed = list_dirs(tmp_path, skip_dirs=frozenset(), follow_symlinks=True)
    assert set(followed.dirs) == {"link", "real"}


def test_a_missing_root_lists_nothing(tmp_path: Path) -> None:
    """An absent source is an empty listing rather than an error."""
    assert list_dirs(tmp_path / "nope", skip_dirs=frozenset()).dirs == ()


def test_a_folder_that_cannot_be_read_marks_the_listing_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unreadable folder is unknown, not empty, so the picker must not claim completeness."""
    import os

    _tree(tmp_path, "a", "b")
    real = os.scandir

    def denied(path: object) -> object:
        if str(path).endswith(os.sep + "a"):
            raise PermissionError(13, "denied")
        return real(path)  # type: ignore[arg-type]

    monkeypatch.setattr("fnd.walk.os.scandir", denied)
    got = list_dirs(tmp_path, skip_dirs=frozenset())
    assert got.truncated
    assert "a" not in got.read


def test_a_missing_root_is_not_a_partial_listing(tmp_path: Path) -> None:
    """Nothing there is a complete answer, unlike something unreadable."""
    assert not list_dirs(tmp_path / "nope", skip_dirs=frozenset()).truncated


def test_a_full_listing_stops_scanning_the_rest_of_the_queue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Once the cap clips a folder's children no later folder can add room, so none is read."""
    import os

    _tree(tmp_path, *[f"d{i}/x" for i in range(5)])
    real = os.scandir
    calls: list[str] = []

    def counting(path: object) -> object:
        calls.append(str(path))
        return real(path)  # type: ignore[arg-type]

    monkeypatch.setattr("fnd.walk.os.scandir", counting)
    got = list_dirs(tmp_path, skip_dirs=frozenset(), max_dirs=3)
    assert got.truncated
    assert len(calls) == 1


def test_an_entry_that_cannot_be_typed_leaves_its_parent_unread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A child whose type is unknown might be a folder, so the parent cannot count as fully listed."""
    import os

    _tree(tmp_path, "a", "b")
    real = os.scandir

    class Entry:
        def __init__(self, entry: os.DirEntry[str]) -> None:
            self._entry = entry
            self.name = entry.name
            self.path = entry.path

        def is_dir(self, *, follow_symlinks: bool = True) -> bool:
            if self.name == "a":
                raise PermissionError(13, "denied")
            return self._entry.is_dir(follow_symlinks=follow_symlinks)

    class Scan:
        def __init__(self, path: object) -> None:
            self._it = real(path)  # type: ignore[arg-type]

        def __enter__(self) -> list[Entry]:
            return [Entry(e) for e in self._it]

        def __exit__(self, *exc: object) -> None:
            self._it.close()

    monkeypatch.setattr("fnd.walk.os.scandir", Scan)
    got = list_dirs(tmp_path, skip_dirs=frozenset())
    assert got.dirs == ("b",)
    assert got.truncated
    assert "" not in got.read
