"""The save gesture is written one way, everywhere.

It was `Ctrl+S` in three places and `^S` in four, on screens a user moves
between in one session; `app._is_commit` normalises both spellings, so the
code already knew. Lowercase, because it sits beside `t`, `c` and `y` hints
and a capital reads as though Shift is wanted.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from fnd.tui.widgets import COMMIT_KEY

_TUI = Path(__file__).resolve().parent.parent / "fnd" / "tui"


def _docstrings(tree: ast.AST) -> set[int]:
    """Ids of the string nodes that are docstrings, which no user reads."""
    out: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        first = node.body[0] if node.body else None
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
            out.add(id(first.value))
    return out


def test_no_module_spells_it_another_way() -> None:
    """Anywhere inside a user-visible string, not only as the whole of one.

    A line-wise check passes with `^S` mid sentence in a notice: a capital
    beside the lowercase `t`, `c`, `y` hints.
    """
    offenders: list[str] = []
    for path in sorted(_TUI.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        skip = _docstrings(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            if id(node) in skip:
                continue
            if re.search(r"Ctrl\+S|\^S", node.value):
                offenders.append(f"{path.name}:{node.lineno}")
    assert not offenders, f"the save key spelled another way: {offenders}"


def test_it_carries_no_capital() -> None:
    assert COMMIT_KEY == COMMIT_KEY.lower(), COMMIT_KEY


def test_the_app_still_recognises_both_spellings() -> None:
    """The control: the footer's fitting code drops anchors before a screen's
    own keys, and it finds the save hint by name."""
    from fnd.tui.app import _is_commit

    assert _is_commit(COMMIT_KEY)
    assert _is_commit("Ctrl+S"), "an older spelling must still be recognised"
