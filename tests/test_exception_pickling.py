"""Every fnd exception survives the pickle its own message depends on.

PDF extraction runs in a process pool, and an exception crosses it by pickle.
``ExtractError`` collapsed its two arguments into a one-element ``args``, so
an encrypted PDF surfaced as ``BrokenProcessPool: a process terminated
abruptly`` (after a teardown, a respawn and a doomed retry) instead of
"encrypted PDF (password required)". The structural test below fails for the
next exception with a custom constructor, whether or not it crosses a pool
today.
"""

from __future__ import annotations

import ast
import copy
import pickle
from pathlib import Path

import pytest

from fnd.config_migrations import ConfigTooNewError
from fnd.extract.base import ExtractError
from fnd.filter_dsl import FilterError
from fnd.query_errors import (
    MissingFilterValueError,
    QuerySyntaxError,
    UnknownFilterValueError,
)

_FND = Path(__file__).resolve().parent.parent / "fnd"


def _raise_encrypted() -> None:
    raise ExtractError("/x/locked.pdf", "encrypted PDF (password required)")


def _is_exception(node: ast.ClassDef) -> bool:
    names = [ast.unparse(b) for b in node.bases]
    return any(n.endswith(("Error", "Exception")) for n in names)


def _unrebuildable_init(node: ast.ClassDef) -> ast.FunctionDef | None:
    """A constructor the default ``cls(*args)`` cannot satisfy.

    ``args`` carries the one formatted message, so a single positional is
    safe and ``__dict__`` restores the rest. More than one, or a required
    keyword-only, is not.
    """
    for item in node.body:
        if not (isinstance(item, ast.FunctionDef) and item.name == "__init__"):
            continue
        a = item.args
        positional = [p.arg for p in (*a.posonlyargs, *a.args) if p.arg != "self"]
        required_kw = [p.arg for p, d in zip(a.kwonlyargs, a.kw_defaults, strict=True) if d is None]
        return item if len(positional) > 1 or required_kw else None
    return None


def _defines_reduce(node: ast.ClassDef) -> bool:
    return any(isinstance(i, ast.FunctionDef) and i.name == "__reduce__" for i in node.body)


def test_every_constructor_args_cannot_satisfy_declares_a_rebuild() -> None:
    missing: list[str] = []
    seen = 0
    for path in sorted(_FND.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef) or not _is_exception(node):
                continue
            seen += 1
            if _unrebuildable_init(node) and not _defines_reduce(node):
                missing.append(f"{path.relative_to(_FND.parent)}:{node.lineno} {node.name}")
    assert seen > 5, "the scan found no exception classes: it has stopped looking"
    assert not missing, (
        "custom __init__ without __reduce__ (unpickles wrong or raises): " + ", ".join(missing)
    )


@pytest.mark.parametrize(
    "error",
    [
        ExtractError("/x/a.pdf", "encrypted PDF (password required)"),
        ConfigTooNewError(12, 9),
        FilterError("unclosed quote", 7),
        QuerySyntaxError("unbalanced bracket", hint="close it"),
        UnknownFilterValueError(
            label="file type", value="pdff", suggestions=["pdf"], known=["pdf", "md"], flag="--kind"
        ),
        MissingFilterValueError(
            label="collection", flag="--collection", proposal="all", known=["notes"]
        ),
    ],
    ids=lambda e: type(e).__name__,
)
def test_round_trip_keeps_the_message_and_the_hint(error: Exception) -> None:
    back = pickle.loads(pickle.dumps(error))
    assert type(back) is type(error)
    assert str(back) == str(error)
    assert getattr(back, "hint", None) == getattr(error, "hint", None)
    assert str(copy.copy(error)) == str(error)


def test_the_reason_survives_the_real_extraction_pool() -> None:
    """The pool is the only reason this matters: the parent unpickles it."""
    import asyncio

    from fnd.extract._worker import run_in_pool, shutdown_pool

    try:
        with pytest.raises(ExtractError) as caught:
            asyncio.run(run_in_pool(_raise_encrypted))
    finally:
        shutdown_pool()
    assert "encrypted PDF (password required)" in str(caught.value)
