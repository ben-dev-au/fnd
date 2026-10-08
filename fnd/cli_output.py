"""Where fnd writes to the terminal. Human-readable text names files, headings,
collections and errors, so it is written as a
:func:`~fnd.display_text.terminal_block`; machine-readable output (JSON) is
already escaped and is written verbatim."""

from __future__ import annotations

import typer

from fnd.display_text import terminal_block

__all__ = ["echo", "echo_data"]


def echo(message: str = "", *, err: bool = False) -> None:
    """Write ``message`` as a terminal block, to stderr when ``err``."""
    typer.echo(terminal_block(message), err=err)


def echo_data(text: str) -> None:
    """Write machine-readable ``text`` to stdout unchanged."""
    typer.echo(text)
