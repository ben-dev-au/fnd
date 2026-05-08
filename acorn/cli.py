"""Acorn CLI entrypoints (typer).

Phases 1-3 surface:

- ``acorn version``
- ``acorn index <root>`` — quick ad-hoc index of a single root into the
  default collection
- ``acorn search "<query>"`` — query the index
- ``acorn config show|edit|validate`` — manage ~/Library/Application Support/acorn/config.toml
- ``acorn collection list|add|rm|reindex`` — manage collections
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import typer

app = typer.Typer(
    name="acorn",
    help="Fast, free, keyboard-driven document search for macOS.",
    no_args_is_help=True,
)

config_app = typer.Typer(name="config", help="Manage acorn's TOML config file.")
collection_app = typer.Typer(name="collection", help="Manage indexed collections.")
app.add_typer(config_app, name="config")
app.add_typer(collection_app, name="collection")


# ── Top-level commands ────────────────────────────────────────────────────


@app.command()
def version() -> None:
    """Print acorn version."""
    from acorn import __version__

    typer.echo(__version__)


@app.command()
def index(
    root: Path,
    collection: str = typer.Option("default", help="Collection to index into."),
) -> None:
    """Index documents under ROOT (ad-hoc, single-root). For configured
    collections use ``acorn collection reindex <name>``."""
    from acorn.config import default_index_dir
    from acorn.index import build_index

    written = build_index(roots=[root], index_dir=default_index_dir(), collection=collection)
    typer.echo(f"indexed {written} chunks under {root} → collection {collection}")


@app.command()
def search(
    query: str,
    limit: int = 10,
    collection: str | None = typer.Option(None, "--collection", "-c"),
) -> None:
    """Search the index and print ranked file:locator snippets to stdout."""
    from acorn.config import default_index_dir
    from acorn.query import Searcher

    searcher = Searcher(index_dir=default_index_dir())
    for hit in searcher.search(query, limit=limit, collection=collection):
        loc = ""
        if hit.page:
            loc = f":p.{hit.page}"
        elif hit.slide:
            loc = f":s.{hit.slide}"
        elif hit.heading_path:
            loc = f" §{hit.heading_path}"
        typer.echo(f"{hit.score:6.3f}  {hit.path}{loc}\n        {hit.snippet}")


# ── config sub-commands ───────────────────────────────────────────────────


@config_app.command("show")
def config_show() -> None:
    """Print the effective merged config as TOML-ish JSON."""
    from acorn.config import load

    cfg = load()
    typer.echo(cfg.model_dump_json(indent=2))


@config_app.command("edit")
def config_edit() -> None:
    """Open the config TOML in $EDITOR; create from template if missing."""
    from acorn.config import STARTER_TEMPLATE, app_data_dir, default_config_path

    path = default_config_path()
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        # default_config_path returns the primary path when neither exists.
        if not path.parent.samefile(app_data_dir()):
            # Fallback path was returned; create primary instead.
            path = app_data_dir() / "config.toml"
            path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(STARTER_TEMPLATE, encoding="utf-8")
        typer.echo(f"wrote starter template to {path}")

    editor = os.environ.get("EDITOR") or os.environ.get("VISUAL") or "vi"
    rc = subprocess.call([editor, str(path)])
    sys.exit(rc)


@config_app.command("validate")
def config_validate() -> None:
    """Validate the config TOML; exit 1 with a helpful message on failure."""
    from acorn.config import default_config_path, load

    path = default_config_path()
    if not path.exists():
        typer.echo(f"no config at {path}", err=True)
        raise typer.Exit(code=1)
    try:
        cfg = load(path)
    except Exception as e:
        typer.echo(f"invalid config: {e}", err=True)
        raise typer.Exit(code=1) from e
    typer.echo(
        f"✓ {path} valid; {len(cfg.collections)} collection(s): "
        f"{', '.join(sorted(cfg.collections)) or '(none)'}"
    )


# ── collection sub-commands ───────────────────────────────────────────────


@collection_app.command("list")
def collection_list() -> None:
    """List configured collections."""
    from acorn.config import load

    cfg = load()
    if not cfg.collections:
        typer.echo("(no collections configured — run `acorn config edit`)")
        return
    for name, c in sorted(cfg.collections.items()):
        marker = " *" if name == cfg.defaults.collection else "  "
        typer.echo(f"{marker} {name}: {len(c.roots)} root(s)")


@collection_app.command("reindex")
def collection_reindex(
    name: str,
    rebuild: bool = typer.Option(False, "--rebuild", help="Drop existing chunks first."),
) -> None:
    """Index (or re-index) a configured collection."""
    from acorn.config import default_index_dir, load
    from acorn.index import build_index_from_config

    cfg = load()
    cc = cfg.collection(name)
    written = build_index_from_config(
        config=cc,
        collection=name,
        index_dir=default_index_dir(),
        rebuild=rebuild,
    )
    typer.echo(f"indexed {written} chunks for collection {name}")
