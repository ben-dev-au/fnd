"""The results title counts in English: `1 file`, not `1 files`.

The title is where this app carries its counts, so it is read constantly.
"""

from __future__ import annotations

import pytest

from fnd.tui.results_view import _count


@pytest.mark.parametrize(
    ("n", "mark", "expected"),
    [
        (1, "", "1 file"),
        (2, "", "2 files"),
        (0, "", "0 files"),
        (50, "+", "50+ files"),
        (1, "+", "1+ files"),
    ],
)
def test_a_count_reads_as_english(n: int, mark: str, expected: str) -> None:
    """A floor is never singular: `1+ file` would claim there is exactly one."""
    assert _count(n, mark, "file") == expected
