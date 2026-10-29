"""fnd reaches into Textual's private API only where no public route exists (#56)."""

from __future__ import annotations

import ast
from pathlib import Path

import textual.widgets.markdown as public_markdown

_SOURCE = Path(__file__).resolve().parent.parent / "fnd"


def _modules() -> list[tuple[Path, ast.Module]]:
    return [(p, ast.parse(p.read_text(encoding="utf-8"))) for p in sorted(_SOURCE.rglob("*.py"))]


def test_tree_rows_are_read_through_the_public_api() -> None:
    """``Tree._tree_lines`` has public stand-ins: ``get_node_at_line`` and ``last_line``."""
    readers = [
        f"{path.relative_to(_SOURCE.parent)}:{node.lineno}"
        for path, module in _modules()
        for node in ast.walk(module)
        if isinstance(node, ast.Attribute) and node.attr == "_tree_lines"
    ]
    assert readers == []


def test_no_markdown_block_is_imported_privately_once_public() -> None:
    """A name ``textual.widgets.markdown`` exports comes from there, not ``_markdown``."""
    public = set(public_markdown.__all__)
    private = [
        f"{path.relative_to(_SOURCE.parent)}:{node.lineno} {alias.name}"
        for path, module in _modules()
        for node in ast.walk(module)
        if isinstance(node, ast.ImportFrom) and node.module == "textual.widgets._markdown"
        for alias in node.names
        if alias.name in public
    ]
    assert private == []
