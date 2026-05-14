"""Config + filesystem locations.

Per plan §6: a single TOML file owned by the user with collections, defaults,
and ranking profiles. ``acorn config edit`` is the read-modify-write entry
point; ``acorn collection add`` appends a new ``[[sources]]`` table via
``tomlkit``, preserving comments and unrelated tables.

Phase 3 covers: defaults, collections (roots/includes/excludes/follow_symlinks).
Phase 7 adds ranking profiles (recency boost / filetype boost / phrase
proximity) — wired into :class:`acorn.rerank.RankingProfile` at search time.
Phase 5.5e-1 adds :class:`SourceConfig` + multi-source collections + the
``frontmatter_filter`` DSL (parsed eagerly via :mod:`acorn.filter_dsl`).
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any, Literal

from platformdirs import user_data_dir
from pydantic import BaseModel, Field, field_validator, model_validator

_APP_NAME = "acorn"


def app_data_dir() -> Path:
    return Path(user_data_dir(_APP_NAME, appauthor=False))


def default_index_dir() -> Path:
    d = app_data_dir() / "index"
    d.mkdir(parents=True, exist_ok=True)
    return d


def default_config_path() -> Path:
    """Resolve the config-file path with a fallback chain.

    Primary: ``$XDG_DATA_HOME/acorn/config.toml`` (or platformdirs equivalent
    on macOS — under Application Support). Fallback: ``~/.config/acorn/config.toml``.
    """
    primary = app_data_dir() / "config.toml"
    if primary.exists():
        return primary
    fallback = Path.home() / ".config" / _APP_NAME / "config.toml"
    if fallback.exists():
        return fallback
    return primary


# ── Schema ──────────────────────────────────────────────────────────────────

# Indexer-supported file types in display order. Used by the Add Source /
# Add Collection wizards to render the Includes multi-select. Keep this in
# sync with the kinds the extractor pipeline handles.
INDEXER_FILETYPES: dict[str, str] = {
    "md": "Markdown (.md)",
    "pdf": "PDF (.pdf)",
    "docx": "Word (.docx)",
    "pptx": "PowerPoint (.pptx)",
    "txt": "Plain text (.txt)",
}

# Exclude presets for the Add Collection wizard's Excludes multi-select. Each
# preset defines a set of globs and a default toggle state. Presets marked
# default=True are pre-ticked in the UI.
EXCLUDES_PRESETS: dict[str, dict[str, Any]] = {
    "hidden": {
        "label": "Hidden / system",
        "globs": ["**/.*", "**/.DS_Store", "**/.git/**"],
        "default": True,
    },
    "node_modules": {
        "label": "Node modules",
        "globs": ["**/node_modules/**"],
        "default": False,
    },
    "python_caches": {
        "label": "Python caches",
        "globs": ["**/__pycache__/**", "**/*.pyc"],
        "default": False,
    },
    "build_artefacts": {
        "label": "Build artefacts",
        "globs": ["**/dist/**", "**/build/**"],
        "default": False,
    },
    "obsidian_meta": {
        "label": "Obsidian metadata",
        "globs": ["**/.obsidian/**"],
        "default": False,
    },
}


class SourceConfig(BaseModel):
    """One root path inside a collection with its own filter chain."""

    path: Path
    includes: list[str] = Field(default_factory=list)
    excludes: list[str] = Field(default_factory=list)
    follow_symlinks: bool = False
    frontmatter_filter: str | None = None

    @field_validator("path", mode="before")
    @classmethod
    def _expand_path(cls, v: object) -> object:
        return Path(str(v)).expanduser()

    @field_validator("frontmatter_filter")
    @classmethod
    def _validate_filter(cls, v: str | None) -> str | None:
        # Eagerly compile so a syntax error surfaces at config load with
        # the parser's column. The compiled predicate is rebuilt on demand
        # at index time — caching here would couple the model to runtime.
        if v is None or not v.strip():
            return None
        from acorn.filter_dsl import FilterError, compile_filter

        try:
            compile_filter(v)
        except FilterError as e:
            raise ValueError(f"frontmatter_filter: {e.message} (col {e.column})") from e
        return v


class CollectionConfig(BaseModel):
    """One named set of sources + collection-wide options.

    A collection can be configured in two equivalent shapes:

    * **New (recommended):** ``[[collections.X.sources]]`` — one TOML table
      per source, each with its own includes/excludes/frontmatter_filter.
    * **Legacy:** flat ``roots = [...]``, ``includes = [...]``,
      ``excludes = [...]`` on the collection. Loader normalises this into
      a single implicit source so downstream code only sees the new shape.

    Mixing both forms on the same collection is rejected at load.
    """

    # New shape — primary going forward.
    sources: list[SourceConfig] = Field(default_factory=list)

    # Legacy shape — accepted for backward compat; reconciled below.
    roots: list[Path] = Field(default_factory=list)
    includes: list[str] = Field(default_factory=list)
    excludes: list[str] = Field(default_factory=list)
    follow_symlinks: bool = False

    ocr: bool = False  # phase 10 honours this
    ranking_profile: str = "default"

    @field_validator("roots", mode="before")
    @classmethod
    def _expand_roots(cls, v: object) -> object:
        if not isinstance(v, list):
            return v
        return [Path(str(p)).expanduser() for p in v]

    @model_validator(mode="after")
    def _normalise_sources(self) -> CollectionConfig:
        # sources were explicitly provided alongside roots — only valid if roots
        # is already empty (idempotent re-validation path is handled below).
        if self.sources and self.roots:
            # Check whether this is an already-normalised model being re-validated
            # (e.g. when a CollectionConfig instance is nested inside Config()).
            # In that case sources already reflect the promoted roots, so roots
            # is stale — just clear it.
            # A user who *intentionally* mixes [[sources]] + roots will have
            # source paths that do NOT match the roots 1-for-1.
            source_paths = {s.path for s in self.sources}
            root_paths = {Path(str(r)).expanduser() for r in self.roots}
            if root_paths <= source_paths:
                # All roots are represented in sources → idempotent re-validation.
                object.__setattr__(self, "roots", [])
                return self
            raise ValueError("collection mixes legacy 'roots' with 'sources'; pick one")
        if not self.sources and self.roots:
            # Promote legacy flat shape into a single implicit source.
            implicit = [
                SourceConfig(
                    path=root,
                    includes=list(self.includes),
                    excludes=list(self.excludes),
                    follow_symlinks=self.follow_symlinks,
                )
                for root in self.roots
            ]
            object.__setattr__(self, "sources", implicit)
            # Clear roots so this model is idempotent across re-validation.
            object.__setattr__(self, "roots", [])
        return self


class RankingProfileConfig(BaseModel):
    """User-facing knobs that map onto :class:`acorn.rerank.RankingProfile`.

    ``bm25_k1`` / ``bm25_b`` are accepted for forward-compat (§21 Spike A:
    Tantivy hardcodes them upstream) and silently ignored at runtime.
    """

    recency_boost: float = 0.0
    recency_half_life: str = "365d"  # parsed via _parse_duration
    filetype_boosts: dict[str, float] = Field(default_factory=dict)
    phrase_proximity: float = 0.0
    proximity_max_window: int = 50
    # forward-compat (currently ignored — see §21 Spike A)
    bm25_k1: float | None = None
    bm25_b: float | None = None


class Defaults(BaseModel):
    collection: str = "default"
    result_limit: int = 200
    preview_chunks: int = 5
    debounce_ms: int = 200
    drill_summary_mode: Literal["always_show", "smart", "always_ellipsis"] = "always_show"
    # Per-file match surfacing. Sections are kept when their relevance
    # score is at least ``sections_score_threshold * file_top_score``,
    # capped at ``sections_per_file_max`` as a safety net. Threshold of
    # 1.0 = top-scoring section only; 0.0 = every match up to the cap.
    sections_score_threshold: float = 0.5
    sections_per_file_max: int = 200
    # Worker thread count for parallel chunk decode during preview load.
    # tantivy's ``Searcher.doc()`` releases the GIL, so threads (not
    # processes) are the right primitive. 1 = serial decode (back-compat
    # fallback). Bump for very large PDFs; lower if CPU contention shows
    # up in profiles.
    preview_decode_workers: int = 4
    # Idle delay before a results-tree cursor move triggers a preview
    # load. Rapid arrow-key sweeps no longer kick off a decode at each
    # intermediate row — only the final position loads, so scrolling
    # down a long results list stays fluid. 0 = load instantly (legacy
    # behaviour). Typical 100–250 ms.
    preview_load_debounce_ms: int = 150
    # Number of top result files to decode + pre-mount widgets for
    # in the background as soon as a search returns. Covers both
    # preview pipelines (flat for PDF/TXT, structural for md/docx/
    # pptx) — a cursor move to any of those files becomes a
    # visibility flip on the pre-mounted widget, not a fresh mount.
    # Bumped from 5 to 10 so tapped navigation through the result
    # list doesn't outpace the prefetcher on small-to-medium files.
    # 0 disables prefetch entirely (useful in tests or on very
    # large corpora where the prefetch wastes work).
    preview_prefetch_count: int = 10


class Config(BaseModel):
    defaults: Defaults = Field(default_factory=Defaults)
    collections: dict[str, CollectionConfig] = Field(default_factory=dict)
    ranking: dict[str, RankingProfileConfig] = Field(default_factory=dict)

    def collection(self, name: str) -> CollectionConfig:
        try:
            return self.collections[name]
        except KeyError as e:
            raise KeyError(
                f"unknown collection {name!r}; defined: {sorted(self.collections)}"
            ) from e

    def ranking_profile(self, name: str) -> RankingProfileConfig:
        """Look up a ranking profile by name. Returns the all-zero default
        when ``name`` is missing — callers can opt out of ranking just by
        not defining a profile."""
        return self.ranking.get(name, RankingProfileConfig())


_DURATION_RE = re.compile(r"^\s*(\d+)\s*([smhd])\s*$")


def parse_duration_seconds(s: str) -> int:
    """Parse a `\\d+[smhd]` duration into seconds. Tolerates whitespace.

    Examples: ``30d`` → 2_592_000; ``12h`` → 43_200; ``365d`` → 31_536_000.
    Used for ``recency_half_life`` in ranking profiles.
    """
    m = _DURATION_RE.match(s)
    if not m:
        raise ValueError(f"invalid duration {s!r}; expected forms like '30d', '12h', '60m'")
    value = int(m.group(1))
    unit = m.group(2)
    return {"s": 1, "m": 60, "h": 3600, "d": 86_400}[unit] * value


# ── Loaders ─────────────────────────────────────────────────────────────────


def load(path: Path | None = None) -> Config:
    """Load and validate the config TOML. If the file is missing, return a
    Config with no collections — the caller decides whether to error."""
    p = path if path is not None else default_config_path()
    if not p.exists():
        return Config()
    raw = tomllib.loads(p.read_text(encoding="utf-8"))
    return Config.model_validate(raw)


def write_collection_source(
    *,
    config_path: Path,
    collection_name: str,
    source: SourceConfig,
) -> None:
    """Append ``source`` to ``collection_name`` in the config TOML at
    ``config_path``. Creates the file (and the collection table) if
    needed. Preserves comments and unrelated tables via tomlkit.

    Raises FileNotFoundError if the parent dir is missing — caller is
    expected to mkdir the config dir.
    """
    import tomlkit

    if config_path.exists():
        doc = tomlkit.parse(config_path.read_text(encoding="utf-8"))
    else:
        doc = tomlkit.document()

    collections = doc.setdefault("collections", tomlkit.table())
    collection = collections.setdefault(collection_name, tomlkit.table())
    sources_array = collection.setdefault("sources", tomlkit.aot())  # array-of-tables

    new_table = tomlkit.table()
    new_table["path"] = str(source.path)
    if source.includes:
        new_table["includes"] = list(source.includes)
    if source.excludes:
        new_table["excludes"] = list(source.excludes)
    if source.follow_symlinks:
        new_table["follow_symlinks"] = source.follow_symlinks
    if source.frontmatter_filter:
        new_table["frontmatter_filter"] = source.frontmatter_filter
    sources_array.append(new_table)

    config_path.write_text(tomlkit.dumps(doc), encoding="utf-8")


def write_collection(
    *,
    config_path: Path,
    name: str,
    collection: CollectionConfig,
) -> None:
    """Replace ``[collections.<name>]`` (and its ``[[sources]]`` array) in
    the TOML at ``config_path``. Comments and unrelated tables are
    preserved via ``tomlkit``. Creates the file and the ``collections``
    table if needed.

    The supplied :class:`CollectionConfig` is the canonical post-validation
    form; this writer emits the new ``[[sources]]`` shape and never the
    legacy flat ``roots = [...]`` shape.
    """
    import tomlkit

    if config_path.exists():
        doc = tomlkit.parse(config_path.read_text(encoding="utf-8"))
    else:
        doc = tomlkit.document()

    collections = doc.setdefault("collections", tomlkit.table())
    new_table = tomlkit.table()
    if collection.ranking_profile != "default":
        new_table["ranking_profile"] = collection.ranking_profile
    if collection.sources:
        sources_aot = tomlkit.aot()
        for source in collection.sources:
            st = tomlkit.table()
            st["path"] = str(source.path)
            if source.includes:
                st["includes"] = list(source.includes)
            if source.excludes:
                st["excludes"] = list(source.excludes)
            if source.follow_symlinks:
                st["follow_symlinks"] = source.follow_symlinks
            if source.frontmatter_filter:
                st["frontmatter_filter"] = source.frontmatter_filter
            sources_aot.append(st)
        new_table["sources"] = sources_aot
    else:
        # tomlkit aot() with zero entries produces no output; use an inline
        # array so `[collections.<name>]` survives the round-trip and loads
        # back as CollectionConfig(sources=[]).
        new_table.add("sources", tomlkit.array())
    collections[name] = new_table
    config_path.write_text(tomlkit.dumps(doc), encoding="utf-8")


def write_setting(*, config_path: Path, dotted_path: str, value: object) -> Config:
    """Update a single field in the config TOML by dotted path.

    Examples of ``dotted_path``:

    - ``defaults.result_limit``
    - ``defaults.collection``
    - ``ranking.default.recency_boost``
    - ``collections.default.ranking_profile``

    Preserves comments and unrelated tables via ``tomlkit``. The full
    document is re-validated through :class:`Config` after the in-memory
    edit; on validation failure the on-disk file is **not** modified and
    the underlying exception propagates so the caller can surface it.
    """
    import tomlkit

    if config_path.exists():
        doc = tomlkit.parse(config_path.read_text(encoding="utf-8"))
    else:
        doc = tomlkit.document()

    parts = [p for p in dotted_path.split(".") if p]
    if not parts:
        raise ValueError("dotted_path must contain at least one segment")
    *parents, leaf = parts
    cursor: object = doc
    for p in parents:
        existing = cursor.get(p) if hasattr(cursor, "get") else None  # type: ignore[union-attr]
        if existing is None or not hasattr(existing, "get"):
            new_tbl = tomlkit.table()
            cursor[p] = new_tbl  # type: ignore[index]
            cursor = new_tbl
        else:
            cursor = existing
    cursor[leaf] = value  # type: ignore[index]

    # Validate the full document before committing to disk. Re-parsing the
    # tomlkit dump gives us a plain dict — Pydantic doesn't accept tomlkit's
    # Item subclasses directly.
    raw = tomllib.loads(tomlkit.dumps(doc))
    config = Config.model_validate(raw)

    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(tomlkit.dumps(doc), encoding="utf-8")
    return config


def delete_collection(*, config_path: Path, name: str) -> None:
    """Remove ``[collections.<name>]`` and its ``[[sources]]`` array from
    the TOML at ``config_path``. Idempotent: silently no-op if the
    collection (or the file) is absent. Comments and unrelated tables
    are preserved via ``tomlkit``."""
    import tomlkit

    if not config_path.exists():
        return
    doc = tomlkit.parse(config_path.read_text(encoding="utf-8"))
    collections = doc.get("collections")
    if not collections or name not in collections:
        return
    del collections[name]
    config_path.write_text(tomlkit.dumps(doc), encoding="utf-8")


CONFIG_TEMPLATE = """\
# Acorn configuration. Edit this file directly, or use the in-app
# Settings menu (open with `:`). Validate with `acorn config validate`.
# UI-driven edits preserve your comments and formatting.

[defaults]
collection    = "default"     # Active collection when --collection is omitted.
result_limit  = 200           # Max results per query (1-1000).
preview_chunks = 5            # Chunks rendered in the preview pane (1-50).
debounce_ms   = 200           # Wait this many ms after the last keystroke (0-2000).
# How drill-in row trailing summaries render in the Settings menu:
#   always_show       (default): each row shows its content summary
#   smart                       : summary only on rows with real content
#   always_ellipsis             : a dim `…` on every drill row
drill_summary_mode = "always_show"
# Per-file match surfacing. Sections in a file are kept when their
# score is at least ``sections_score_threshold * file_top_score``,
# capped by ``sections_per_file_max``. 0.5 keeps strong-relative-score
# matches and drops weak ones; raise toward 1.0 for fewer/stronger hits,
# lower toward 0.0 to surface every match (subject to the cap).
sections_score_threshold = 0.5    # 0.0-1.0
sections_per_file_max    = 200    # Hard cap (1-2000)
# Preview decode parallelism. tantivy releases the GIL for doc reads, so
# threads help on huge PDFs. 1 = serial; raise (4-8) for big files.
preview_decode_workers   = 4      # 1-16
# Idle delay before a results-tree cursor move triggers a preview load.
# Rapid arrow-key sweeps skip intermediate rows; only the final position
# loads. 0 = load instantly. Typical range 100-250.
preview_load_debounce_ms = 150    # ms, 0-1000
# Number of top result files to decode + pre-mount widgets for in the
# background as soon as a search returns. Covers both flat (PDF/TXT)
# and structural (md/docx/pptx) previews. 0 disables prefetch.
preview_prefetch_count   = 10     # 0-20

# A collection groups one or more source directories. The starter
# collection points at ~/Documents; edit, add more [[sources]] tables,
# or replace it entirely.
[[collections.default.sources]]
path = "~/Documents"
# includes = ["**/*.md", "**/*.pdf", "**/*.docx", "**/*.pptx", "**/*.txt"]
excludes = ["**/.git/**", "**/.DS_Store", "**/__pycache__/**"]
# follow_symlinks = false
# frontmatter_filter = "type:note"  # md sources only — DSL described in docs.

# Example second collection — uncomment to use:
# [[collections.notes.sources]]
# path = "~/Documents/Notes"
# includes = ["*.md", "*.txt"]
# excludes = [".obsidian/**", "drafts/**"]

# Ranking profiles tune the scorer. Attach to a collection by setting
# ranking_profile = "<name>" inside that collection's table.
# [ranking.default]
# recency_boost      = 0.2       # 0.0 = ignore mtime; higher = boost recent files.
# recency_half_life  = "365d"
# filetype_boosts    = { md = 1.0, txt = 0.9, pdf = 0.85 }
# phrase_proximity   = 0.3
"""

# Backwards-compat alias for any older callers that imported the
# previous name. Both refer to the same string.
STARTER_TEMPLATE = CONFIG_TEMPLATE
