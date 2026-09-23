"""No footer chip paints a reversed blank slot where its key should be.

The chip renderer interpolates into Rich markup, so a key holding `[` is parsed
as a tag and vanishes. A search for an empty key string cannot find it: the
string is not empty, Rich eats it.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from fnd.tui.app import render_hint_bar


def _painted(pairs: tuple[tuple[str, str], ...]) -> str:
    return render_hint_bar((), pairs).plain


def test_a_bracketed_key_survives_the_renderer() -> None:
    assert "[key]" in _painted((("[key]", "Run directly"),))


def test_a_bracketed_label_survives_too() -> None:
    assert "[x]" in _painted((("a", "[x] marks it"),))


def test_an_ordinary_chip_is_unchanged() -> None:
    """The control: escaping must not alter what already worked."""
    painted = _painted((("⏎", "Run"), ("Esc", "Back")))

    assert "⏎" in painted
    assert "Run" in painted
    assert "Esc" in painted


@pytest.mark.parametrize("module", ["fnd/tui/settings_screen.py", "fnd/tui/menu.py"])
def test_no_hint_table_holds_an_empty_key(module: str) -> None:
    """Class-wide: a chip with no key is a reversed blank the user cannot press."""
    source = Path(module).read_text(encoding="utf-8")
    empty: list[tuple[int, tuple[str, str]]] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Tuple) or len(node.elts) != 2:
            continue
        try:
            pair = ast.literal_eval(node)
        except Exception:
            continue
        if (
            isinstance(pair, tuple)
            and len(pair) == 2
            and all(isinstance(x, str) for x in pair)
            and pair[1]
            and not pair[0]
        ):
            empty.append((node.lineno, pair))
    assert not empty, f"hint pairs with no key: {empty}"
