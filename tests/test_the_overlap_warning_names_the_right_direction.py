"""The check ran both ways and the sentence was written for one of them.

Adding `/vault` when `/vault/notes` is already a source said "This folder is
already inside '/vault/notes'", which is backwards: `/vault` contains it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fnd.config import SourceConfig, overlapping_source


def test_a_child_added_under_a_parent_reads_as_inside(tmp_path: Path) -> None:
    parent = tmp_path / "vault"
    child = parent / "notes"
    child.mkdir(parents=True)

    other, contains = overlapping_source([SourceConfig(path=parent)], SourceConfig(path=child))

    assert other, "the overlap is still detected"
    assert contains is False, "the new source is inside the existing one"


def test_a_parent_added_over_a_child_does_not_read_as_inside(tmp_path: Path) -> None:
    parent = tmp_path / "vault"
    child = parent / "notes"
    child.mkdir(parents=True)

    other, contains = overlapping_source([SourceConfig(path=child)], SourceConfig(path=parent))

    assert other, "the overlap is still detected"
    assert contains is True, "the new source contains the existing one"


def test_no_overlap_says_nothing(tmp_path: Path) -> None:
    """The control: unrelated folders must not report a relation."""
    a = tmp_path / "one"
    b = tmp_path / "two"
    a.mkdir()
    b.mkdir()

    other, contains = overlapping_source([SourceConfig(path=a)], SourceConfig(path=b))

    assert other == ""
    assert contains is False


@pytest.mark.parametrize("wording", ["already inside", "already covers"])
def test_both_doors_carry_both_sentences(wording: str) -> None:
    """The TUI and the CLI print the same warning, so both need both forms."""
    tui = Path("fnd/tui/settings_screen.py").read_text(encoding="utf-8")
    cli = Path("fnd/cli.py").read_text(encoding="utf-8")

    assert wording in tui, f"the TUI cannot say {wording!r}"
    assert wording in cli, f"the CLI cannot say {wording!r}"
