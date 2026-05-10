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

from acorn.config import default_config_path, default_index_dir

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
    from acorn.index import build_index

    written = build_index(roots=[root], index_dir=default_index_dir(), collection=collection)
    typer.echo(f"indexed {written} chunks under {root} → collection {collection}")


@app.command()
def tui(
    collection: str | None = typer.Option(None, "--collection", "-c"),
    query: str = typer.Option("", "--query", "-q", help="Initial query to seed the TUI."),
) -> None:
    """Launch the interactive TUI."""
    from acorn.config import load
    from acorn.migrate import prompt_and_rebuild_or_exit
    from acorn.tui import AcornApp

    cfg = load()
    prompt_and_rebuild_or_exit(index_dir=default_index_dir(), config=cfg)

    AcornApp(collection=collection, initial_query=query, config=cfg).run()


@app.command()
def search(
    query: str,
    limit: int = 10,
    collection: str | None = typer.Option(None, "--collection", "-c"),
    meta: str | None = typer.Option(
        None, "--meta", help="Inline metadata-filter DSL (md hits only)."
    ),
    explain: int | None = typer.Option(
        None,
        "--explain",
        help=(
            "Print JSON trace for the Nth hit (1-indexed). Routes through "
            "the regime-aware layered search; emits which regime fired "
            "(strong-signal / fusion / cascade), per-sub-query BM25 stats, "
            "and per-hit RRF contributions."
        ),
    ),
) -> None:
    """Search the index and print ranked file:locator snippets to stdout.

    With ``--explain N``, emits a JSON trace for hit N after the regular
    rows, showing which regime fired and the per-hit score breakdown.
    """
    import json

    from acorn.config import load
    from acorn.filter_dsl import FilterError
    from acorn.layered import search_layered
    from acorn.migrate import prompt_and_rebuild_or_exit
    from acorn.query import Hit, Searcher

    prompt_and_rebuild_or_exit(index_dir=default_index_dir(), config=load())

    searcher = Searcher(index_dir=default_index_dir())
    try:
        if explain is None:
            hits = searcher.search(query, limit=limit, collection=collection, metadata_filter=meta)
            for hit in hits:
                _print_hit(hit)
            return
        groups, trace = search_layered(
            searcher,
            query=query,
            limit=limit,
            sections_per_file=5,
            collection=collection,
            metadata_filter=meta,
            with_trace=True,
        )
        # Flatten groups → hits in display order; one row per matched section.
        flat: list[Hit] = [h for g in groups for h in g.hits]
        for hit in flat[:limit]:
            _print_hit(hit)
        if not (1 <= explain <= len(flat)):
            typer.echo(
                f"--explain {explain}: out of range (have {len(flat)} hits)",
                err=True,
            )
            raise typer.Exit(code=1)
        payload = trace.to_json()
        target = flat[explain - 1]
        payload["explained_hit"] = {
            "index": explain,
            "parent_id": target.parent_id,
            "chunk_seq": target.chunk_seq,
            "score": round(target.score, 4),
        }
        typer.echo(json.dumps(payload, indent=2))
    except FilterError as e:
        typer.echo(f"invalid filter: {e.message} (col {e.column})", err=True)
        raise typer.Exit(code=1) from e


def _print_hit(hit: object) -> None:
    """Render one hit row in the existing CLI format."""
    loc = ""
    page = getattr(hit, "page", 0)
    slide = getattr(hit, "slide", 0)
    heading_path = getattr(hit, "heading_path", "")
    if page:
        loc = f":p.{page}"
    elif slide:
        loc = f":s.{slide}"
    elif heading_path:
        loc = f" §{heading_path}"
    score = getattr(hit, "score", 0.0)
    path = getattr(hit, "path", "")
    snippet = getattr(hit, "snippet", "")
    typer.echo(f"{score:6.3f}  {path}{loc}\n        {snippet}")


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
    from pydantic import ValidationError

    from acorn.config import default_config_path, load

    path = default_config_path()
    if not path.exists():
        typer.echo(f"no config at {path}", err=True)
        raise typer.Exit(code=1)
    try:
        cfg = load(path)
    except ValidationError as e:
        # Pydantic packs the column-aware FilterError message into the
        # individual error's ``msg`` field. Surface it line-by-line so the
        # user sees both the location (frontmatter_filter) and column.
        for err in e.errors():
            loc = ".".join(str(x) for x in err["loc"])
            typer.echo(f"{loc}: {err['msg']}", err=True)
        raise typer.Exit(code=1) from e
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
        typer.echo(f"{marker} {name}: {len(c.sources)} source(s)")


@collection_app.command("add")
def collection_add(
    name: str = typer.Argument(..., help="Collection name."),
    source: list[Path] = typer.Option(
        ...,
        "--source",
        help="Root directory for this collection. Repeat to add multiple.",
    ),
    include: list[str] = typer.Option(
        [],
        "--include",
        help="Glob to include. Repeatable. Defaults to all supported types.",
    ),
    exclude: list[str] = typer.Option(
        [],
        "--exclude",
        help="Glob to exclude. Repeatable.",
    ),
    filter: str | None = typer.Option(
        None,
        "--filter",
        help="Frontmatter filter DSL (md sources only).",
    ),
    follow_symlinks: bool = typer.Option(False, "--follow-symlinks/--no-follow-symlinks"),
) -> None:
    """Add (or extend) a collection in the user's config TOML.

    Each invocation appends one source (one --source argument). Repeat
    the command to add additional sources to the same collection.
    """
    from acorn.config import (
        SourceConfig,
        write_collection_source,
    )
    from acorn.filter_dsl import FilterError, compile_filter

    if filter is not None:
        try:
            compile_filter(filter)
        except FilterError as e:
            typer.echo(f"invalid filter: {e.message} (col {e.column})", err=True)
            raise typer.Exit(code=1) from e

    if len(source) != 1:
        typer.echo("--source must be specified exactly once per command", err=True)
        raise typer.Exit(code=1)

    cfg_path = default_config_path()
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    new_source = SourceConfig(
        path=source[0],
        includes=list(include),
        excludes=list(exclude),
        follow_symlinks=follow_symlinks,
        frontmatter_filter=filter,
    )
    write_collection_source(config_path=cfg_path, collection_name=name, source=new_source)
    typer.echo(f"added source {source[0]} to collection {name} in {cfg_path}")


@collection_app.command("reindex")
def collection_reindex(
    name: str,
    rebuild: bool = typer.Option(False, "--rebuild", help="Drop existing chunks first."),
) -> None:
    """Index (or re-index) a configured collection."""
    from acorn.config import load
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
