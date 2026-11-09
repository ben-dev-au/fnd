"""Name the TUI form when a query that is a subcommand name fails to parse.

``fnd <query>`` opens the TUI, but a first word that names a subcommand runs
that subcommand (``fnd search -c notes`` is ``search`` missing its query), and
shell quotes cannot change it because they never reach fnd. When the
subcommand refuses its arguments and ``tui`` would accept them, the usage
error ends with the ``fnd tui ...`` command that opens them as a query.

Typer re-exports none of Click's parsing classes, so this module is the one
place that reaches into ``typer._click``.
"""

from __future__ import annotations

import shlex
from typing import Any

from typer._click.core import Command, Context
from typer._click.exceptions import UsageError
from typer.core import TyperCommand, TyperGroup


def tui_form(parent: Context | None, words: list[str]) -> str | None:
    """The hint line for ``words`` as a TUI launch, or None if ``tui`` refuses them."""
    if parent is None or not isinstance(parent.command, TyperGroup):
        return None
    tui = parent.command.get_command(parent, "tui")
    if tui is None:
        return None
    try:
        tui.make_context("tui", list(words), parent=parent)
    except UsageError:
        return None
    return (
        f"Searching for {words[0]!r}? It is an fnd command, so open the TUI with:\n"
        f"  {parent.command_path} tui {shlex.join(words)}"
    )


class _TuiHint(Command):
    def make_context(
        self,
        info_name: str | None,
        args: list[str],
        parent: Context | None = None,
        **extra: Any,
    ) -> Context:
        # Click's parser consumes ``args`` in place.
        words = [info_name or "", *args]
        try:
            return super().make_context(info_name, args, parent=parent, **extra)
        except UsageError as e:
            if hint := tui_form(parent, words):
                e.message = f"{e.message}\n{hint}"
            raise


class TuiHintCommand(_TuiHint, TyperCommand):
    """A top-level command whose usage errors offer the TUI form."""


class TuiHintGroup(_TuiHint, TyperGroup):
    """A top-level command group whose usage errors offer the TUI form."""
