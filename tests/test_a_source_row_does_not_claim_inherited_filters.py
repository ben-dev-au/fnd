"""Every source row read `… · tags` while its own detail screen said `inherited`.

`_other_filters` asked `effective_filters`, which resolves the defaults in, and
the shipped `no_index` exclusion is a default, so the chip landed on every row
in the app and distinguished none of them.
"""

from __future__ import annotations

from pathlib import Path

from fnd.config import SourceConfig, SourceFilters
from fnd.tui.menu import _other_filters


def test_a_bare_source_does_not_claim_the_defaults_as_its_own(tmp_path: Path) -> None:
    named = _other_filters(SourceConfig(path=tmp_path))

    assert "tags" not in named, f"the shipped no_index exclusion is not this source's: {named}"


def test_it_still_says_the_source_is_narrowed(tmp_path: Path) -> None:
    """The row exists because an inherited rule can cut a source to nothing.

    Naming the dimensions as the source's own was the lie; staying silent
    about them would be the opposite one.
    """
    named = _other_filters(SourceConfig(path=tmp_path))

    assert "inherited" in named, named


def test_a_source_with_its_own_rule_names_it(tmp_path: Path) -> None:
    named = _other_filters(SourceConfig(path=tmp_path, filters=SourceFilters(max_size=1000)))

    assert "size" in named, named


def test_two_sources_that_differ_read_differently(tmp_path: Path) -> None:
    """The point of the row: it has to tell them apart."""
    bare = _other_filters(SourceConfig(path=tmp_path))
    own = _other_filters(SourceConfig(path=tmp_path, excludes=["build/**"]))

    assert bare != own, (bare, own)
