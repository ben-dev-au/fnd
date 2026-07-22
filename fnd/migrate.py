"""Schema-migration helpers (§5.5e-2 close-out).

When ``SCHEMA_VERSION`` bumps, existing on-disk indexes have a stale
sidecar. The runtime gates in :func:`fnd.index._ensure_index` and
:func:`fnd.query._open_index` raise a clear error, but the user has to
see the error first then go run a rebuild command. These helpers let
read-side CLI commands detect the stale state up front and offer to
rebuild, so a fresh upgrade isn't a roadblock.
"""

from __future__ import annotations

import os
import sys
from enum import Enum
from pathlib import Path

import typer

from fnd.config import Config
from fnd.index import build_index_from_config
from fnd.schema import SCHEMA_VERSION

_SIDECAR_NAME = ".fnd-schema-version"


class SchemaStatus(Enum):
    READY = "ready"  # sidecar matches SCHEMA_VERSION
    STALE = "stale"  # sidecar exists, version mismatch
    EMPTY = "empty"  # sidecar doesn't exist (no index yet)


def check_schema_status(index_dir: Path) -> tuple[SchemaStatus, str | None]:
    """Return ``(status, existing_version_string_or_None)``.

    ``existing_version_string`` is None for READY / EMPTY; for STALE it is
    the raw text content of the sidecar (so callers can show it in
    error messages — including when the sidecar is garbled).

    The sidecar is the cheap first signal, but Tantivy stores the schema
    in ``meta.json`` too — if a prior rebuild bumped the sidecar but
    crashed before Tantivy committed new segments, the sidecar lies. So
    when the sidecar matches we additionally try opening the index; on
    Tantivy ``ValueError`` we report STALE (with ``"inconsistent"`` as the
    existing version) so the caller treats it as a rebuild trigger.
    """
    sidecar = index_dir / _SIDECAR_NAME
    if not sidecar.exists():
        return SchemaStatus.EMPTY, None
    text = sidecar.read_text(encoding="utf-8").strip()
    if text != str(SCHEMA_VERSION):
        return SchemaStatus.STALE, text
    # Sidecar says current; verify Tantivy agrees.
    try:
        from tantivy import Index

        from fnd.schema import build_schema

        Index(build_schema(), path=str(index_dir.expanduser().resolve()))
    except ValueError as e:
        if "schema" in str(e).lower():
            return SchemaStatus.STALE, "inconsistent"
        raise
    return SchemaStatus.READY, None


def prompt_and_rebuild_or_exit(
    *,
    index_dir: Path,
    config: Config,
    is_tty: bool | None = None,
) -> None:
    """Read-side CLI helper: detect schema state, prompt to rebuild on
    TTY, exit 1 with a clear command on non-TTY.

    A READY index is a no-op. An EMPTY index prints "no index here yet"
    and exits 1 (the user must build first). A STALE index prompts on
    TTY; on confirm, every collection in ``config`` is rebuilt in place.

    ``is_tty`` is exposed for tests; defaults to ``sys.stdin.isatty()``.
    """
    status, existing = check_schema_status(index_dir)
    if status is SchemaStatus.READY:
        return
    if status is SchemaStatus.EMPTY:
        typer.echo(
            f"no index at {index_dir}. Configure a collection with "
            f"`fnd collection add <name> --source <path>` "
            f"then run `fnd collection reindex <name>`.",
            err=True,
        )
        raise typer.Exit(code=1)

    # STALE.
    typer.echo(
        f"index at {index_dir} has schema v{existing}; current is v{SCHEMA_VERSION}.",
        err=True,
    )
    if is_tty is None:
        # Allow tests to force the TTY path.
        interactive = True if os.environ.get("_FND_FORCE_TTY") == "1" else sys.stdin.isatty()
    else:
        interactive = is_tty

    if not interactive:
        typer.echo(
            "Re-run with `fnd collection reindex <name> --rebuild` "
            "for each collection in your config, then retry.",
            err=True,
        )
        raise typer.Exit(code=1)

    if not config.collections:
        typer.echo(
            "no collections configured. Run `fnd collection add <name> "
            "--source <path>` then `fnd collection reindex <name>`.",
            err=True,
        )
        raise typer.Exit(code=1)

    if not typer.confirm("Rebuild all collections now?", default=True):
        typer.echo("aborted. Re-run when you're ready to rebuild.", err=True)
        raise typer.Exit(code=1)

    for name, cc in sorted(config.collections.items()):
        typer.echo(f"Rebuilding collection {name}…")
        n = build_index_from_config(
            config=cc,
            collection=name,
            index_dir=index_dir,
            rebuild=True,
            tag_sources=tuple(config.defaults.tag_sources),
            tag_frontmatter_keys=tuple(config.defaults.tag_frontmatter_keys),
        )
        typer.echo(f"  {n} chunks indexed.")
