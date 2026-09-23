"""Two identical sources keep a short label rather than the same full path.

They can never be told apart by their path (they ARE the same path), so
deepening buys nothing and costs the summary column: two rows of one long
shared prefix with no room for what either one filters. The row number is what
distinguishes them.
"""

from __future__ import annotations

from pathlib import Path

from fnd.tui.menu import _source_labels


def test_duplicates_stay_short() -> None:
    assert _source_labels(["/a/b/papers", "/a/b/papers"]) == ["papers", "papers"]


def test_genuinely_different_paths_still_separate() -> None:
    """The control, and the reason the deepening exists at all."""
    assert _source_labels(["/a/b/papers", "/c/d/papers"]) == [
        str(Path("b/papers")),
        str(Path("d/papers")),
    ]


def test_a_duplicate_does_not_stop_its_neighbours_separating() -> None:
    """The mixed case: one pair is identical, another pair is not."""
    labels = _source_labels(["/a/b/papers", "/a/b/papers", "/c/d/papers", "/x/notes"])

    assert labels[0] == labels[1], "identical paths, identical labels"
    assert labels[0] != labels[2], "and still distinct from the one that differs"
    assert labels[3] == "notes", "an unshared name stays bare"


def test_one_source_is_just_its_name() -> None:
    assert _source_labels(["/deep/nested/vault"]) == ["vault"]
