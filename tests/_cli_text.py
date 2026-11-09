"""Read Typer output as plain prose, whatever terminal styling it carries."""

from __future__ import annotations

import re

from rich.text import Text

#: The Unicode Box Drawing block: Rich draws rounded panels, legacy Windows square ones.
_BOX = re.compile("[\u2500-\u257f]")


def plain_cli_text(output: str) -> str:
    """``output`` without ANSI codes or Rich box characters, whitespace collapsed.

    Typer forces colour under GITHUB_ACTIONS, and a style code or panel edge can split a phrase.
    """
    return " ".join(_BOX.sub(" ", Text.from_ansi(output).plain).split())
