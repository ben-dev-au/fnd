"""Adding a source inside, above or equal to another is pointed out.

The index deduplicates the overlap (11 docs, not 12), so it is harmless, and it
is also pointless: a source that indexes nothing new reads exactly like one
that does.
"""

from __future__ import annotations

from pathlib import Path

from fnd.config import SourceConfig, overlapping_source


def _src(path: Path) -> SourceConfig:
    return SourceConfig(path=path)


def test_a_child_of_an_existing_source_is_named(tmp_path: Path) -> None:
    parent = tmp_path / "vault"
    (parent / "notes").mkdir(parents=True)

    found, _contains = overlapping_source([_src(parent)], _src(parent / "notes"), None)

    assert found == str(parent)


def test_a_parent_of_an_existing_source_is_named(tmp_path: Path) -> None:
    """The other direction: the new source swallows one already there."""
    parent = tmp_path / "vault"
    (parent / "notes").mkdir(parents=True)

    found, _contains = overlapping_source([_src(parent / "notes")], _src(parent), None)

    assert found == str(parent / "notes")


def test_the_same_folder_twice_is_named(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    root.mkdir()

    assert overlapping_source([_src(root)], _src(root), None)[0]


def test_a_sibling_is_not(tmp_path: Path) -> None:
    """The control: two unrelated folders must say nothing."""
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()

    assert not overlapping_source([_src(tmp_path / "a")], _src(tmp_path / "b"), None)[0]


def test_editing_a_source_does_not_flag_itself(tmp_path: Path) -> None:
    """The trap: the row being edited is still in the list it is compared to."""
    root = tmp_path / "vault"
    root.mkdir()

    assert not overlapping_source([_src(root)], _src(root), 0)[0]


def test_a_missing_folder_is_not_a_crash(tmp_path: Path) -> None:
    """Paths are resolved, and a source can name a folder that is not there."""
    assert not overlapping_source([_src(tmp_path / "gone")], _src(tmp_path / "also-gone"), None)[0]
