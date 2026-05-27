"""FND CLI entrypoints (typer).

Phases 1-3 surface:

- ``fnd version``
- ``fnd index <root>`` — quick ad-hoc index of a single root into the
  default collection
- ``fnd search "<query>"`` — query the index
- ``fnd config show|edit|validate`` — manage ~/Library/Application Support/fnd/config.toml
- ``fnd collection list|add|rm|reindex`` — manage collections
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import typer

from fnd.config import default_config_path, default_index_dir

_ROOT_HELP = """Fast, free, keyboard-driven document search for macOS.

Usage:
  fnd                          Launch the interactive TUI.
  fnd <query>                  Launch the TUI with <query> pre-filled.
  fnd -c <collection> <query>  Launch the TUI scoped to a collection.
  fnd <command> <args>         Run a subcommand (see below).

Note: if <query> matches a subcommand name (e.g. `version`), the
subcommand wins. Use `fnd tui <query>` or type the query inside the TUI.
"""


app = typer.Typer(name="fnd", help=_ROOT_HELP)

config_app = typer.Typer(name="config", help="Manage fnd's TOML config file.")
collection_app = typer.Typer(name="collection", help="Manage indexed collections.")
extras_app = typer.Typer(name="extras", help="Manage opt-in feature extras.")
cache_app = typer.Typer(name="cache", help="Manage the on-disk PDF Texture Cache.")
app.add_typer(config_app, name="config")
app.add_typer(collection_app, name="collection")
app.add_typer(extras_app, name="extras")
app.add_typer(cache_app, name="cache")


# Keep in sync with the @app.command() / app.add_typer() registrations below.
_KNOWN_SUBCOMMANDS = frozenset(
    {"version", "index", "tui", "search", "config", "collection", "extras", "cache"}
)
_ROOT_FLAGS = frozenset({"--help", "-h", "--install-completion", "--show-completion"})


def _rewrite_default_command(argv: list[str]) -> list[str]:
    """Route bare `fnd` and `fnd <free-text>` to the `tui` subcommand.

    Subcommands always win on exact collision. Root flags pass through.
    Anything else gets `tui` prepended so subcommand-level options like
    `--collection` are parsed by `tui`, not the root group.
    """
    if not argv:
        return ["tui"]
    head = argv[0]
    if head in _ROOT_FLAGS or head in _KNOWN_SUBCOMMANDS:
        return argv
    return ["tui", *argv]


def main() -> None:
    """Console-script entry point: rewrite argv, then dispatch to Typer."""
    if sys.platform != "darwin":
        print(
            "fnd targets macOS only for now — it relies on macOS file APIs and the "
            "`open` URL handler. Linux/Windows support isn't available yet.",
            file=sys.stderr,
        )
        raise SystemExit(1)
    app(args=_rewrite_default_command(sys.argv[1:]))


# ── Top-level commands ────────────────────────────────────────────────────


@app.command()
def version() -> None:
    """Print fnd version."""
    from fnd import __version__

    typer.echo(__version__)


@app.command()
def index(
    root: Path,
    collection: str = typer.Option("default", help="Collection to index into."),
) -> None:
    """Index documents under ROOT (ad-hoc, single-root). For configured
    collections use ``fnd collection reindex <name>``."""
    from fnd.index import build_index

    written = build_index(roots=[root], index_dir=default_index_dir(), collection=collection)
    typer.echo(f"indexed {written} chunks under {root} → collection {collection}")


@app.command()
def tui(
    query: list[str] = typer.Argument(
        default_factory=list, help="Initial query to seed the TUI.", show_default=False
    ),
    collection: str | None = typer.Option(None, "--collection", "-c"),
    query_opt: str = typer.Option("", "--query", "-q", hidden=True),
) -> None:
    """Launch the interactive TUI."""
    from fnd.config import default_config_path, load
    from fnd.migrate import prompt_and_rebuild_or_exit
    from fnd.tui import FNDApp
    from fnd.tui.config_recovery_screen import run_recovery

    initial_query = " ".join(query) if query else query_opt

    # Loop so the user can fix the config in-place and immediately retry.
    while True:
        try:
            cfg = load()
            break
        except Exception as e:
            if not run_recovery(e, default_config_path()):
                raise typer.Exit(code=1) from e

    prompt_and_rebuild_or_exit(index_dir=default_index_dir(), config=cfg)

    # Spawn the PDF extraction worker before Textual's run() rewires
    # stdin/stderr. macOS multiprocessing.spawn validates fds_to_keep
    # against the current FD table; once Textual has registered the
    # alt-screen and signal-wakeup pipes, that validation fails and
    # every PDF in an indexer chain dies as ExtractError. Warming the
    # pool here captures the clean FD state.
    from fnd.extract._worker import warm_pool

    warm_pool()

    # mouse=True keeps the driver able to re-enable capture at runtime; the
    # app disables it on mount unless defaults.clickable_interface is set.
    FNDApp(collection=collection, initial_query=initial_query, config=cfg).run(mouse=True)


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

    from fnd.config import load
    from fnd.filter_dsl import FilterError
    from fnd.layered import search_layered
    from fnd.migrate import prompt_and_rebuild_or_exit
    from fnd.query import Hit, Searcher

    cfg = load()
    prompt_and_rebuild_or_exit(index_dir=default_index_dir(), config=cfg)

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
            auto_fuzzy_enabled=cfg.defaults.fuzzy_enabled,
            min_term_chars=cfg.defaults.fuzzy_min_term_chars,
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
    from fnd.config import load

    cfg = load()
    typer.echo(cfg.model_dump_json(indent=2))


@config_app.command("path")
def config_path() -> None:
    """Print the path to the config TOML (whether or not it exists yet)."""
    typer.echo(str(default_config_path()))


@config_app.command("edit")
def config_edit() -> None:
    """Open the config TOML in $EDITOR; create from template if missing."""
    from fnd.config import CONFIG_TEMPLATE, app_data_dir, default_config_path

    path = default_config_path()
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        # default_config_path returns the primary path when neither exists.
        if not path.parent.samefile(app_data_dir()):
            # Fallback path was returned; create primary instead.
            path = app_data_dir() / "config.toml"
            path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(CONFIG_TEMPLATE, encoding="utf-8")
        typer.echo(f"wrote starter template to {path}")

    editor = os.environ.get("EDITOR") or os.environ.get("VISUAL") or "vi"
    rc = subprocess.call([editor, str(path)])
    sys.exit(rc)


@config_app.command("validate")
def config_validate() -> None:
    """Validate the config TOML; exit 1 with a helpful message on failure."""
    from pydantic import ValidationError

    from fnd.config import default_config_path, load

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
    from fnd.config import load

    cfg = load()
    if not cfg.collections:
        typer.echo("(no collections configured — run `fnd config edit`)")
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
    from fnd.config import (
        InvalidCollectionNameError,
        SourceConfig,
        validate_collection_name,
        write_collection_source,
    )
    from fnd.filter_dsl import FilterError, compile_filter

    try:
        validate_collection_name(name)
    except InvalidCollectionNameError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(code=1) from e

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
    from fnd.config import load
    from fnd.index import build_index_from_config

    cfg = load()
    cc = cfg.collection(name)
    written = build_index_from_config(
        config=cc,
        collection=name,
        index_dir=default_index_dir(),
        rebuild=rebuild,
    )
    typer.echo(f"indexed {written} chunks for collection {name}")


# ---- extras ---------------------------------------------------------------


def _format_disk(mb: int) -> str:
    if mb >= 1024:
        return f"~{mb / 1024:.1f} GB"
    return f"~{mb} MB"


@extras_app.command("list")
def extras_list() -> None:
    """List all available extras and their installed status."""
    from fnd.extras import EXTRAS, is_extra_installed

    if not EXTRAS:
        typer.echo("no extras defined")
        return
    for extra in EXTRAS.values():
        status = "installed" if is_extra_installed(extra) else "not installed"
        typer.echo(f"{extra.name}  [{status}]  {extra.description}")


@extras_app.command("status")
def extras_status() -> None:
    """Show installed extras with disk usage."""
    from fnd.extras import EXTRAS, actual_disk_mb, installed_packages, is_extra_installed

    for extra in EXTRAS.values():
        installed = is_extra_installed(extra)
        if not installed:
            typer.echo(f"{extra.name}: not installed")
            continue
        disk = actual_disk_mb(extra)
        typer.echo(f"{extra.name}: installed ({_format_disk(disk)})")
        for pkg in installed_packages(extra):
            typer.echo(f"  - {pkg.display}")


def _print_install_disclosure(extra) -> None:  # type: ignore[no-untyped-def]
    total_mb = sum(p.disk_mb for p in extra.packages)
    typer.echo(f"\nInstall '{extra.name}' — {extra.description}\n")
    typer.echo("Will install:")
    for p in extra.packages:
        typer.echo(f"  - {p.display}  (~{p.disk_mb} MB)")
    typer.echo(f"\nApproximate total disk + download: {_format_disk(total_mb)}")
    typer.echo("ML model weights (a portion of the size above) download on first use.")
    typer.echo("")
    # For the pdf-structure extra, also disclose the indexing-time cost
    # the user will incur on the first big reindex after install.
    if extra.name == "pdf-structure":
        typer.echo(
            "Indexing-time impact:\n"
            "  After installing, your next `fnd collection reindex` will spend\n"
            "  ~30s per PDF extracting structure (one-time per file; cached\n"
            "  thereafter). Rough scale: 100 books ~50 min, 500 books ~4 hours.\n"
            "  Subsequent reindexes only re-process changed files.\n"
            "  Indexing can run in the background and auto-resumes if interrupted.\n"
        )
    typer.echo("Without this extra, PDFs continue to render as flat text (current behaviour).\n")


def _print_uninstall_disclosure(extra) -> None:  # type: ignore[no-untyped-def]
    from fnd.extras import actual_disk_mb, installed_packages

    typer.echo(f"\nUninstall '{extra.name}' — {extra.description}\n")
    typer.echo("Will remove:")
    for p in installed_packages(extra):
        typer.echo(f"  - {p.display}")
    for c in extra.cache_dirs:
        if c.exists():
            typer.echo(f"  - cache: {c}")
    typer.echo(f"\nApproximate disk recovered: {_format_disk(actual_disk_mb(extra))}")
    typer.echo(
        "Already-indexed structured chunks remain in the index — previews keep\n"
        "working. New extractions revert to flat text. To fully revert existing\n"
        "collections, run `fnd collection reindex <name>` after uninstall.\n"
    )


def _require_uv() -> None:
    """Extras install/uninstall shell out to ``uv``. A Homebrew/pipx
    end-user install won't necessarily have it on PATH, so fail with an
    actionable message instead of a raw ``FileNotFoundError`` traceback
    from subprocess."""
    import shutil

    if shutil.which("uv") is None:
        typer.echo(
            "Structured extras are installed via `uv`, which isn't on your PATH.\n"
            "Install it with `brew install uv` (or see https://docs.astral.sh/uv/),\n"
            "then re-run this command.",
            err=True,
        )
        raise typer.Exit(code=1)


@extras_app.command("install")
def extras_install(
    name: str = typer.Argument(..., help="Extra to install (e.g. 'pdf-structure')"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt."),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Print the commands that would run; don't execute."
    ),
) -> None:
    """Install an opt-in extra after a disk-impact disclosure prompt."""
    from fnd.extras import EXTRAS, install_commands, run_command

    extra = EXTRAS.get(name)
    if extra is None:
        typer.echo(f"unknown extra: {name!r}; available: {list(EXTRAS)}", err=True)
        raise typer.Exit(code=2)

    _print_install_disclosure(extra)
    cmds = install_commands(extra)
    if dry_run:
        for c in cmds:
            typer.echo("would run: " + " ".join(c))
        return
    _require_uv()
    if not yes and not typer.confirm("Continue?", default=False):
        typer.echo("aborted")
        raise typer.Exit(code=1)
    # Flip default-groups before sync runs so the install persists
    # across subsequent ``uv sync`` calls.
    _toggle_default_group_for_extra(name, present=True)
    for c in cmds:
        typer.echo("$ " + " ".join(c))
        rc, _stdout, stderr = run_command(c)
        if rc != 0:
            typer.echo(f"command failed (exit {rc}):\n{stderr}", err=True)
            raise typer.Exit(code=rc)
    typer.echo(f"\nInstalled {name}. Run `fnd collection reindex <name>` to apply.")


@extras_app.command("uninstall")
def extras_uninstall(
    name: str = typer.Argument(..., help="Extra to uninstall (e.g. 'pdf-structure')"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print commands; don't execute."),
) -> None:
    """Remove an opt-in extra. Indexed chunks remain; new extractions revert."""
    from fnd.extras import EXTRAS, run_command, uninstall_commands

    extra = EXTRAS.get(name)
    if extra is None:
        typer.echo(f"unknown extra: {name!r}; available: {list(EXTRAS)}", err=True)
        raise typer.Exit(code=2)

    _print_uninstall_disclosure(extra)
    # --dry-run is a planning preview — show the full plan as if
    # everything were installed. The real uninstall (below) uses the
    # filtered version that skips already-removed packages.
    if dry_run:
        for c in uninstall_commands(extra, assume_installed=True):
            typer.echo("would run: " + " ".join(c))
        return
    cmds = uninstall_commands(extra)
    if not cmds:
        typer.echo(f"\n{name} is not currently installed — nothing to do.")
        return
    _require_uv()
    if not yes and not typer.confirm("Continue?", default=False):
        typer.echo("aborted")
        raise typer.Exit(code=1)
    # Drop default-groups membership first; uv sync --no-group then
    # removes the packages and leaves the project unsubscribed.
    _toggle_default_group_for_extra(name, present=False)
    for c in cmds:
        typer.echo("$ " + " ".join(c))
        rc, _stdout, stderr = run_command(c)
        if rc != 0:
            typer.echo(f"command failed (exit {rc}):\n{stderr}", err=True)
            raise typer.Exit(code=rc)
    typer.echo(f"\nUninstalled {name}.")


def _toggle_default_group_for_extra(name: str, *, present: bool) -> None:
    """If fnd is running inside a uv-managed project venv, flip the
    extra's PEP-735 group in ``[tool.uv] default-groups`` so the
    install survives subsequent ``uv sync`` calls. No-op when there's
    no owning pyproject.toml (uv tool install / system Python case)."""
    import sys

    from fnd.extras import (
        _project_pyproject_for_python,  # type: ignore[attr-defined]
        disable_pdf_structure_default_group,
        enable_pdf_structure_default_group,
    )

    if name != "pdf-structure":
        return  # only pdf-structure has a group toggle wired today
    pyproject = _project_pyproject_for_python(sys.executable)
    if pyproject is None:
        return
    if present:
        enable_pdf_structure_default_group(pyproject)
    else:
        disable_pdf_structure_default_group(pyproject)


# ---- cache ----------------------------------------------------------------


def _human_bytes(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    kb = n / 1024
    if kb < 1024:
        return f"{kb:.1f} KB"
    mb = kb / 1024
    if mb < 1024:
        return f"{mb:.1f} MB"
    gb = mb / 1024
    return f"{gb:.2f} GB"


@cache_app.command("status")
def cache_status() -> None:
    """Show PDF Texture Cache location, count of saved texturings, and size."""
    from fnd.cache import ExtractionCache, default_cache_dir

    cache = ExtractionCache()
    root = default_cache_dir()
    if not root.exists():
        typer.echo(f"PDF Texture Cache: {root}  (not yet created)")
        return
    typer.echo(f"PDF Texture Cache:  {root}")
    typer.echo(f"saved texturings:   {cache.entry_count()}")
    typer.echo(f"disk used:          {_human_bytes(cache.total_size_bytes())}")


@cache_app.command("clear")
def cache_clear(
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation."),
) -> None:
    """Forget every saved texturing. Next Update index will texturise every PDF."""
    import shutil

    from fnd.cache import default_cache_dir

    root = default_cache_dir()
    if not root.exists():
        typer.echo("PDF Texture Cache is empty (no directory)")
        return

    if not yes:
        typer.echo(f"About to remove {root} and every saved texturing.")
        typer.echo("Next Update index will texturise every PDF from scratch.")
        if not typer.confirm("Continue?", default=False):
            typer.echo("aborted")
            raise typer.Exit(code=1)
    shutil.rmtree(root)
    typer.echo(f"removed {root}")


@cache_app.command("prune")
def cache_prune(
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation."),
    dry_run: bool = typer.Option(False, "--dry-run", help="List candidates; don't delete."),
) -> None:
    """Remove cache entries from older texture-engine versions.

    Reads each entry's filename to extract its texture-signature
    suffix, compares against the current signature, and offers to
    remove the stale ones.
    """
    from fnd.cache import default_cache_dir
    from fnd.extract.pdf import texture_signature

    root = default_cache_dir()
    if not root.exists():
        typer.echo("cache is empty (no directory)")
        return

    current = texture_signature()
    stale: list[Path] = []
    fresh = 0
    for shard in root.iterdir():
        if not shard.is_dir():
            continue
        for entry in shard.glob("*.json"):
            stem = entry.stem  # <sha256>--<extractor_signature>
            try:
                _content, _, sig = stem.partition("--")
            except ValueError:
                continue
            if sig == current:
                fresh += 1
            else:
                stale.append(entry)

    typer.echo(f"current texture signature: {current}")
    typer.echo(f"fresh entries (kept):        {fresh}")
    typer.echo(f"stale entries (candidates):  {len(stale)}")

    if not stale:
        return
    if dry_run:
        for p in stale[:10]:
            typer.echo(f"  would remove: {p.name}")
        if len(stale) > 10:
            typer.echo(f"  …and {len(stale) - 10} more")
        return

    if not yes and not typer.confirm(f"Remove {len(stale)} stale entries?", default=False):
        typer.echo("aborted")
        raise typer.Exit(code=1)
    for p in stale:
        try:
            p.unlink()
        except OSError as e:
            typer.echo(f"failed to remove {p}: {e}", err=True)
    typer.echo(f"removed {len(stale)} entries")


@cache_app.command("info")
def cache_info(path: Path) -> None:
    """Show whether a file has a cached extraction (and which key it uses)."""
    from fnd.cache import ExtractionCache, sha256_file
    from fnd.extract.pdf import texture_signature

    if not path.exists():
        typer.echo(f"file not found: {path}", err=True)
        raise typer.Exit(code=2)

    cache = ExtractionCache()
    sig = texture_signature()
    sha = sha256_file(path)
    key = cache.build_key(content_sha256=sha, extractor_signature=sig)
    entry = cache.entry_path(key)
    typer.echo(f"path:                 {path}")
    typer.echo(f"sha256:               {sha[:16]}…")
    typer.echo(f"texture signature:    {sig}")
    typer.echo(f"cache entry:          {entry}")
    typer.echo(f"status:               {'HIT' if entry.exists() else 'MISS'}")
