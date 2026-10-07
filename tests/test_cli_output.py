"""``fnd.cli_output``: human-readable CLI text reaches the terminal as a
:func:`~fnd.display_text.terminal_block`, machine-readable output verbatim, and
nothing in fnd writes to the terminal any other way."""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from fnd.cli import _print_hit
from fnd.cli_output import echo, echo_data
from fnd.display_text import terminal_block, terminal_line
from tests import _hostile_text

FND = Path(__file__).resolve().parents[1] / "fnd"


@pytest.mark.parametrize("raw", _hostile_text.ALL)
def test_echo_writes_a_terminal_block(raw: str, capsys: pytest.CaptureFixture[str]) -> None:
    echo(raw)
    echo(raw, err=True)
    out, err = capsys.readouterr()
    assert out == f"{terminal_block(raw)}\n"
    assert err == f"{terminal_block(raw)}\n"


def test_echo_data_is_written_verbatim(capsys: pytest.CaptureFixture[str]) -> None:
    echo_data('{"name": "a\\u001bb"}')
    assert capsys.readouterr().out == '{"name": "a\\u001bb"}\n'


@pytest.mark.parametrize("raw", _hostile_text.ALL)
def test_a_hit_row_stays_two_terminal_lines(raw: str, capsys: pytest.CaptureFixture[str]) -> None:
    hit = SimpleNamespace(score=1.0, path=f"/n/{raw}.md", heading_path=raw, snippet=raw)
    _print_hit(hit)
    row, snippet = capsys.readouterr().out.removesuffix("\n").split("\n")
    assert row == f" 1.000  {terminal_line(f'/n/{raw}.md')} §{terminal_line(raw)}"
    assert snippet == f"        {terminal_line(raw)}"


def _writes(module: ast.Module) -> list[int]:
    lines: list[int] = []
    for node in ast.walk(module):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id == "print":
            lines.append(node.lineno)
        elif isinstance(func, ast.Attribute) and func.attr in {"echo", "secho", "write"}:
            owner = func.value
            if isinstance(owner, ast.Name) and owner.id in {"typer", "click"}:
                lines.append(node.lineno)
            elif isinstance(owner, ast.Attribute) and owner.attr in {"stdout", "stderr"}:
                lines.append(node.lineno)
    return lines


# The seam itself, and the docling helper's stdout, which is an IPC channel.
_WRITERS = {FND / "cli_output.py", FND / "extract" / "_docling_helper.py"}


def test_nothing_writes_to_the_terminal_except_through_cli_output() -> None:
    found = [
        f"{path.relative_to(FND.parent)}:{line}"
        for path in sorted(FND.rglob("*.py"))
        if path not in _WRITERS
        for line in _writes(ast.parse(path.read_text(encoding="utf-8")))
    ]
    assert found == []
