"""Acorn CLI entrypoints (typer).

Phase 1 surface: `acorn index <root>` and `acorn search "<query>"`. Later phases add
collection management, TUI, watch daemon, etc.
"""

from __future__ import annotations

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
def index(root: str) -> None:
    """Index documents under <root> into the default collection."""
    from acorn.index_pipeline import index_root

    index_root(root)


@app.command()
def search(query: str, limit: int = 10) -> None:
    """Search the index and print ranked file:page snippets to stdout."""
    from acorn.query import search_text

    for hit in search_text(query, limit=limit):
        typer.echo(hit)
