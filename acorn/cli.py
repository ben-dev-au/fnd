"""Acorn CLI entrypoints (typer).

Phase 1 surface: ``acorn version``, ``acorn index <root>``, ``acorn search "<query>"``.
Later phases add collection management, TUI, watch daemon, etc.
"""

from __future__ import annotations

from pathlib import Path

import typer

app = typer.Typer(
    name="acorn",
    help="Fast, free, keyboard-driven document search for macOS.",
    no_args_is_help=True,
)


@app.command()
def version() -> None:
    """Print acorn version."""
    from acorn import __version__

    typer.echo(__version__)


@app.command()
def index(root: Path) -> None:
    """Index documents under ROOT into the default collection."""
    from acorn.config import default_index_dir
    from acorn.index import build_index

    written = build_index(roots=[root], index_dir=default_index_dir())
    typer.echo(f"indexed {written} chunks under {root}")


@app.command()
def search(query: str, limit: int = 10) -> None:
    """Search the index and print ranked file:page snippets to stdout."""
    from acorn.query import search_text

    for hit in search_text(query, limit=limit):
        typer.echo(hit)
