"""A source cut down by `build/**` read as unfiltered.

The row names the dimensions narrowing a source (types, tags, size, dates, a
typed rule), and excludes were missing, though they drop files before any of
the others run.
"""

from __future__ import annotations

from pathlib import Path

from fnd.config import SourceConfig
from fnd.tui.menu import _other_filters


def test_an_excluding_source_says_so(tmp_path: Path) -> None:
    src = SourceConfig(path=tmp_path, excludes=["build/**"])

    assert "excludes" in _other_filters(src)


def test_a_source_without_them_does_not_say_it(tmp_path: Path) -> None:
    """The control: a chip on every row would say nothing about any of them.

    A bare source still names `tags`: the shipped `no_index` exclusion is a
    real rule and it is inherited by everything.
    """
    src = SourceConfig(path=tmp_path)

    assert "excludes" not in _other_filters(src)


def test_it_sits_beside_the_others(tmp_path: Path) -> None:
    from fnd.config import SourceFilters

    src = SourceConfig(
        path=tmp_path,
        excludes=["build/**"],
        filters=SourceFilters(max_size=1000),
    )

    named = _other_filters(src)

    assert "excludes" in named
    assert "size" in named
