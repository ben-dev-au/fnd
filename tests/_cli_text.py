"""Read Typer output as plain prose, whatever terminal styling it carries."""

from __future__ import annotations

import re

from rich.text import Text

_BOX = re.compile(r"[│─╭╮╰╯]")


def plain_cli_text(output: str) -> str:
    """``output`` without ANSI codes or Rich box characters, whitespace collapsed.

    Typer forces colour under GITHUB_ACTIONS, and a style code or panel edge can split a phrase.
    """
    return " ".join(_BOX.sub(" ", Text.from_ansi(output).plain).split())
