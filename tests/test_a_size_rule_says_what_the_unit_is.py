"""`file.size < 200kb` failed with "unexpected token 'kb'" and nothing else.

The DSL has no units (sizes are bytes), and the escape hatch for a bound the
presets cannot express is exactly where a user reaches for `kb`. The presets
start at 1 MB, so anyone wanting a smaller cap arrives here.
"""

from __future__ import annotations

import pytest

from fnd.filters.text_form import parse_or_error


@pytest.mark.parametrize("text", ["file.size < 200kb", "file.size < 200 MB", "file.size < 5 GiB"])
def test_a_unit_suffix_names_the_unit(text: str) -> None:
    _spec, err = parse_or_error(text)

    assert err is not None
    assert "bytes" in err.message, err.message


def test_an_unrelated_syntax_error_is_untouched() -> None:
    """The control: the hint fires on units, not on every leftover token."""
    _spec, err = parse_or_error("file.kind == 'pdf' zzz")

    assert err is not None
    assert "bytes" not in err.message, err.message


def test_the_byte_form_it_points_at_actually_parses() -> None:
    """A hint naming a spelling that does not work would be its own defect."""
    spec, err = parse_or_error("file.size <= 200000")

    assert err is None
    assert spec is not None
    assert spec.max_size == 200000
