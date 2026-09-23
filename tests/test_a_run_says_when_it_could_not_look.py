"""A run that could not read a source reported `Done.`, like a healthy one.

That sameness is what let the destructive version survive: with the prune
ungated, `chmod 000` emptied a collection and the screen said `Done. 0 / 0
files`. The guard now keeps the files, and this is the run saying so, because
a silent rescue reads exactly like a run that had nothing to do.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Iterator
from pathlib import Path

import pytest
from rich.text import Text

from fnd.tui.indexer_modal import _format_indexed_line


def _plain(*args: object, **kw: object) -> str:
    return Text.from_markup(_format_indexed_line(*args, **kw)).plain  # type: ignore[arg-type]


def test_the_line_names_an_unreadable_source() -> None:
    """`0 new  48 already` alone is what a healthy no-op run says too."""
    line = _plain(0, 48, 0, 0, (), ("/Users/x/vault",))

    assert "1 folder unreadable" in line, line


def test_it_counts_them() -> None:
    """Plural, because two dead shares is a different problem from one."""
    line = _plain(0, 48, 0, 0, (), ("/a", "/b"))

    assert "2 folders unreadable" in line, line


def test_a_healthy_run_says_nothing_about_it() -> None:
    """The control: a warning on every run is a warning nobody reads."""
    line = _plain(4, 44, 0, 2, ())

    assert "unreadable" not in line, line
    assert "2 removed" in line, line


def test_the_warning_comes_before_the_counts() -> None:
    """It changes what the other numbers MEAN: nothing was pruned, and the
    source contributed no files. The compact row clips from the right."""
    line = _plain(0, 48, 0, 0, (), ("/a",), compact=True)

    assert line.index("unreadable") < line.index("48 already"), line


@pytest.fixture
def unreadable(tmp_path: Path) -> Iterator[Path]:
    d = tmp_path / "locked"
    d.mkdir()
    (d / "note.md").write_text("content\n", encoding="utf-8")
    os.chmod(d, 0o000)
    try:
        yield d
    finally:
        os.chmod(d, stat.S_IRWXU)


def test_the_runner_reports_the_source_it_could_not_list(unreadable: Path) -> None:
    """End to end through the predicate the prune gate uses, so the message and
    the gate cannot disagree about which root stopped it."""
    from fnd.index import unreadable_roots

    if os.access(unreadable, os.R_OK):
        pytest.skip("running as a user that bypasses directory permissions")

    assert [str(r) for r in unreadable_roots([unreadable])] == [str(unreadable)]
