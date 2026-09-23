"""The pane named `Archive/**` as a path it skips, and counted the files in it.

Two configs that yield 7 files and 11 files printed the same `· 6` and `· 5`,
so the number tracked neither. Excluding a folder is the first thing the
Excludes help text advertises, and it is the only rule on that screen whose
effect the counts did not reflect.
"""

from __future__ import annotations

from pathlib import Path

from fnd.filters.scan import sample_source


def _corpus(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "keep1.md").write_text("# A\n", encoding="utf-8")
    (root / "keep2.md").write_text("# B\n", encoding="utf-8")
    (root / "keep.txt").write_text("t\n", encoding="utf-8")
    archive = root / "Archive"
    archive.mkdir()
    (archive / "old1.md").write_text("# C\n", encoding="utf-8")
    (archive / "old2.md").write_text("# D\n", encoding="utf-8")
    (archive / "old.txt").write_text("t\n", encoding="utf-8")


def test_an_excluded_folder_is_not_counted(tmp_path: Path) -> None:
    _corpus(tmp_path / "src")

    sample = sample_source(tmp_path / "src", excludes=["Archive/**"])

    assert sample.kinds.get("md") == 2, sample.kinds
    assert sample.kinds.get("txt") == 1, sample.kinds


def test_without_the_exclude_the_same_files_are_counted(tmp_path: Path) -> None:
    """The control: two materially different configs printed one number, so
    the test has to show the number moving."""
    _corpus(tmp_path / "src")

    sample = sample_source(tmp_path / "src")

    assert sample.kinds.get("md") == 4, sample.kinds
    assert sample.kinds.get("txt") == 2, sample.kinds
