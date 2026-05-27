"""Config + filesystem locations.

Per plan §6: a single TOML file owned by the user with collections, defaults,
and ranking profiles. ``fnd config edit`` is the read-modify-write entry
point; ``fnd collection add`` appends a new ``[[sources]]`` table via
``tomlkit``, preserving comments and unrelated tables.

Phase 3 covers: defaults, collections (roots/includes/excludes/follow_symlinks).
Phase 7 adds ranking profiles (recency boost / filetype boost / phrase
proximity) — wired into :class:`fnd.rerank.RankingProfile` at search time.
Phase 5.5e-1 adds :class:`SourceConfig` + multi-source collections + the
``frontmatter_filter`` DSL (parsed eagerly via :mod:`fnd.filter_dsl`).
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any, Literal

from platformdirs import user_data_dir
from pydantic import BaseModel, Field, field_validator, model_validator

_APP_NAME = "fnd"


def app_data_dir() -> Path:
    return Path(user_data_dir(_APP_NAME, appauthor=False))


def default_index_dir() -> Path:
    from fnd._perms import secure_mkdir

    return secure_mkdir(app_data_dir() / "index")


def default_config_path() -> Path:
    """Resolve the config-file path with a fallback chain.

    Primary: ``$XDG_DATA_HOME/fnd/config.toml`` (or platformdirs equivalent
    on macOS — under Application Support). Fallback: ``~/.config/fnd/config.toml``.
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
    """One root path inside a collection with its own filter chain.

    Optional ``app`` / ``app_for`` / ``app_params`` fields wire this
    source into the apps registry. See :mod:`fnd.apps` for resolution
    semantics. Validation of app id existence happens at the top-level
    :class:`Config` model_validator — sources can't see siblings.
    """

    path: Path
    includes: list[str] = Field(default_factory=list)
    excludes: list[str] = Field(default_factory=list)
    follow_symlinks: bool = False
    frontmatter_filter: str | None = None
    # When set, this app id is used for any of its declared ``handles``
    # — sugar for the common single-app case.
    app: str | None = None
    # Per-filetype override: ``{"md": "obsidian", "pdf": "skim"}``.
    # Wins against ``app`` per-kind.
    app_for: dict[str, str] = Field(default_factory=dict)
    # Free-form template-variable bag, surfaced as ``{vault}`` etc. in
    # apps registry templates. Common keys: ``vault`` (Obsidian vault
    # name).
    app_params: dict[str, str] = Field(default_factory=dict)

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
        from fnd.filter_dsl import FilterError, compile_filter

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
    """User-facing knobs that map onto :class:`fnd.rerank.RankingProfile`.

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


class AppConfig(BaseModel):
    """User-extensible app entry from ``[apps.<id>]``.

    Exactly one of ``argv`` (process exec) and ``url`` (deep-link via
    ``open <url>``) must be set. Template variables use ``{name}``-style
    placeholders; see ``docs/apps/README.md`` for the full variable list.
    Built-in apps (``system``, ``preview``, ``skim``, ``pdf_expert``,
    ``obsidian``, ``vscode``) ship in :mod:`fnd.apps` and do NOT appear
    here unless the user is overriding them.
    """

    display_name: str
    handles: list[str]
    argv: list[str] | None = None
    url: str | None = None
    notes: str = ""

    @model_validator(mode="after")
    def _argv_xor_url(self) -> AppConfig:
        if (self.argv is None) == (self.url is None):
            raise ValueError("AppConfig: exactly one of argv or url must be set")
        return self

    @field_validator("handles")
    @classmethod
    def _validate_handles(cls, v: list[str]) -> list[str]:
        # Mirror fnd.apps.ALLOWED_HANDLES. Duplicated here so the config
        # layer doesn't depend on importing apps.py at load time (apps.py
        # imports from opener which used to import config — keeping the
        # dependency one-way).
        allowed = {"md", "markdown", "txt", "pdf", "pptx", "docx", "*"}
        for h in v:
            if h not in allowed:
                raise ValueError(f"unknown handle kind {h!r}; allowed: {sorted(allowed)}")
        if not v:
            raise ValueError("handles must be non-empty")
        return v


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
    preview_prefetch_count: int = 4
    # Auto-fuzzy matching in the cascade fallback. When False, only
    # per-term ``~N`` modifiers in the query trigger fuzzy expansion.
    fuzzy_enabled: bool = True
    # Minimum post-stem length for auto-fuzzy. Below this, the term is
    # exact-only. Lucene-AUTO already returns distance 0 for ≤2 char
    # stems, so values 0-3 are no-ops vs current behavior; 4+ extends
    # the floor.
    fuzzy_min_term_chars: int = 3
    # Auto-resume an interrupted reindex on app launch. When True (the
    # default), an existing state file from a previous quit / crash /
    # Ctrl+C resumes silently in the background. False disables the
    # behaviour entirely; the user must trigger the reindex manually.
    indexer_auto_resume: bool = True
    # Populate the PDF structure cache during Update index runs. True
    # (default when pdf-structure is installed) writes fresh entries for
    # any PDF without one. False reads from the cache when entries exist
    # but doesn't write new ones — fast flat-text refresh, useful on
    # battery or slow CPUs.
    cache_at_index_time: bool = True
    # Capture the mouse for a clickable interface (click-to-focus, wheel
    # scroll, scrollbar drag). OFF by default so the terminal owns text
    # selection — drag-select, right-click Copy, ⌘C and macOS
    # Speak-selection all work like any normal terminal app. ON trades
    # that native selection away for mouse interaction.
    clickable_interface: bool = False
    # IN DEVELOPMENT — paint match-position markers on the preview
    # scrollbar track. Accurate on the flat path (PDF/txt) and small,
    # fully-mounted markdown; large markdown lazy-mounts a chunk window
    # so its scroll track spans only part of the file and markers drift.
    # Default off until that's resolved; opt in to preview the feature.
    scrollbar_match_highlight: bool = False


class Config(BaseModel):
    defaults: Defaults = Field(default_factory=Defaults)
    collections: dict[str, CollectionConfig] = Field(default_factory=dict)
    ranking: dict[str, RankingProfileConfig] = Field(default_factory=dict)
    # User-extensible app registry. Keys are app ids referenced by
    # ``app_defaults`` and (Phase 2) per-source app fields. Built-in
    # apps live in :mod:`fnd.apps`; entries here override built-ins on
    # id collision.
    apps: dict[str, AppConfig] = Field(default_factory=dict)
    # Global per-filetype default app. Keys are file kinds (``pdf``,
    # ``md``, ``txt``, ...); values are app ids resolved against
    # ``BUILTIN_APPS | self.apps``. Missing entries fall through to the
    # system default at open time.
    app_defaults: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_app_refs(self) -> Config:
        # Import here to avoid a circular import at module load time.
        from fnd.apps import ALLOWED_HANDLES, APP_ID_RE, BUILTIN_APPS

        known_ids = set(BUILTIN_APPS) | set(self.apps)
        for app_id in self.apps:
            if not APP_ID_RE.fullmatch(app_id):
                raise ValueError(f"invalid app id {app_id!r}: must match {APP_ID_RE.pattern}")
        for kind, app_id in self.app_defaults.items():
            if kind not in ALLOWED_HANDLES or kind == "*":
                raise ValueError(
                    f"app_defaults: unknown filetype {kind!r}; "
                    f"allowed: {sorted(ALLOWED_HANDLES - {'*'})}"
                )
            if app_id not in known_ids:
                raise ValueError(
                    f"app_defaults.{kind} = {app_id!r}: unknown app id (known: {sorted(known_ids)})"
                )
        # Per-source app references — same id-existence rule applies.
        for coll_name, coll in self.collections.items():
            for idx, src in enumerate(coll.sources):
                where = f"collections.{coll_name}.sources[{idx}]"
                if src.app is not None and src.app not in known_ids:
                    raise ValueError(
                        f"{where}.app = {src.app!r}: unknown app id (known: {sorted(known_ids)})"
                    )
                for kind, app_id in src.app_for.items():
                    if kind not in ALLOWED_HANDLES or kind == "*":
                        raise ValueError(
                            f"{where}.app_for: unknown filetype {kind!r}; "
                            f"allowed: {sorted(ALLOWED_HANDLES - {'*'})}"
                        )
                    if app_id not in known_ids:
                        raise ValueError(
                            f"{where}.app_for.{kind} = {app_id!r}: unknown app id "
                            f"(known: {sorted(known_ids)})"
                        )
        return self

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


# Collection names appear as TOML table keys (`[collections.<name>]`),
# as ``--collection`` CLI args, and inside the query DSL ``c:<name>``
# shorthand. Validation is *write-side only*: any name that fails this
# regex is refused before it can enter the config TOML, so names that
# would corrupt the TOML key syntax, escape the DSL splitter on `,`,
# or trip filesystem-component pitfalls (slashes, NUL, leading dot)
# can never appear in saved state.
#
# The DSL parser (`fnd/query_dsl.py::_expand_collection_shorthand`)
# intentionally accepts a slightly broader character set on the
# read side — it's a freeform shorthand and any non-matching name
# just yields no results from Tantivy. Keeping the DSL permissive
# means a typo'd `c:` token doesn't silently get dropped from the
# query.
_COLLECTION_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_-]{0,63}$")


class InvalidCollectionNameError(ValueError):
    """Raised when a collection name fails :func:`validate_collection_name`."""


def validate_collection_name(name: str) -> str:
    """Return ``name`` if it matches the canonical regex, else raise.

    The pattern is intentionally narrow (64 chars max, alphanumeric +
    underscore + hyphen, must not start with hyphen). Names round-trip
    safely through TOML, command-line argv, and the ``c:<name>`` DSL
    shorthand."""
    if not _COLLECTION_NAME_RE.fullmatch(name):
        raise InvalidCollectionNameError(
            f"invalid collection name {name!r}: must match {_COLLECTION_NAME_RE.pattern}"
        )
    return name


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

    validate_collection_name(collection_name)
    if config_path.exists():
        doc = tomlkit.parse(config_path.read_text(encoding="utf-8"))
    else:
        doc = tomlkit.document()

    collections = doc.setdefault("collections", tomlkit.table())
    collection = collections.setdefault(collection_name, tomlkit.table())
    sources_array = collection.setdefault("sources", tomlkit.aot())  # array-of-tables

    new_table = _source_to_tomlkit_table(source)
    sources_array.append(new_table)

    from fnd._perms import secure_write_text

    secure_write_text(config_path, tomlkit.dumps(doc))


def _source_to_tomlkit_table(source: SourceConfig) -> Any:
    """Serialise ``source`` to a tomlkit Table. Single point that owns
    the source-field → TOML mapping so :func:`write_collection_source`,
    :func:`write_collection`, and :func:`clone_source` all preserve the
    same set of optional fields (including the Phase 2 app refs).
    """
    import tomlkit

    table = tomlkit.table()
    table["path"] = str(source.path)
    if source.includes:
        table["includes"] = list(source.includes)
    if source.excludes:
        table["excludes"] = list(source.excludes)
    if source.follow_symlinks:
        table["follow_symlinks"] = source.follow_symlinks
    if source.frontmatter_filter:
        table["frontmatter_filter"] = source.frontmatter_filter
    if source.app is not None:
        table["app"] = source.app
    if source.app_for:
        table["app_for"] = dict(source.app_for)
    if source.app_params:
        table["app_params"] = dict(source.app_params)
    return table


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

    validate_collection_name(name)
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
            sources_aot.append(_source_to_tomlkit_table(source))
        new_table["sources"] = sources_aot
    # An empty `[collections.<name>]` table survives the round-trip and
    # loads back as CollectionConfig(sources=[]) — no inline placeholder
    # needed. A previous `sources = []` workaround broke
    # write_collection_source's later append (tomlkit can't promote an
    # inline array to an array-of-tables in place).
    collections[name] = new_table
    from fnd._perms import secure_write_text

    secure_write_text(config_path, tomlkit.dumps(doc))


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

    from fnd._perms import secure_mkdir, secure_write_text

    secure_mkdir(config_path.parent)
    secure_write_text(config_path, tomlkit.dumps(doc))
    return config


def clone_source(
    *,
    config_path: Path,
    source_collection: str,
    source_index: int,
    target_collection: str,
) -> int:
    """Deep-copy a source from one collection to another.

    Loads the validated :class:`Config`, looks up
    ``[collections.<source_collection>.sources][source_index]``, and
    appends an independent copy to ``[collections.<target_collection>
    .sources]``. Returns the new index in the target. Raises if either
    collection is missing or the index is out of range.

    The clone is a true deep copy — edits to the new entry don't
    propagate back to the original. Uses :func:`_source_to_tomlkit_table`
    so every persisted field (including Phase 2 ``app`` / ``app_for`` /
    ``app_params``) round-trips.
    """
    validate_collection_name(source_collection)
    validate_collection_name(target_collection)
    if source_collection == target_collection:
        raise ValueError("source and target collections must differ")
    cfg = load(config_path)
    if source_collection not in cfg.collections:
        raise KeyError(f"unknown source collection {source_collection!r}")
    if target_collection not in cfg.collections:
        raise KeyError(f"unknown target collection {target_collection!r}")
    sources = cfg.collections[source_collection].sources
    if not 0 <= source_index < len(sources):
        raise IndexError(
            f"source_index {source_index} out of range for "
            f"{source_collection!r} ({len(sources)} sources)"
        )
    source = sources[source_index]
    # Re-validate through SourceConfig so the in-memory copy is fully
    # independent of the original (avoids accidental list/dict aliasing
    # if a caller later mutates the source). Pydantic's model_dump +
    # model_validate gives us a clean deep copy.
    cloned = SourceConfig.model_validate(source.model_dump())
    write_collection_source(
        config_path=config_path,
        collection_name=target_collection,
        source=cloned,
    )
    return len(cfg.collections[target_collection].sources)


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
    from fnd._perms import secure_write_text

    secure_write_text(config_path, tomlkit.dumps(doc))


CONFIG_TEMPLATE = """\
# FND configuration. Edit this file directly, or use the in-app
# Settings menu (open with `:`). Validate with `fnd config validate`.
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
preview_prefetch_count   = 4      # 0-20
# Auto-fuzzy in the cascade fallback. Toggle from the TUI with the
# `toggle_fuzzy` action (default ctrl+f). Per-term `~1` / `~2` in the
# query overrides this — works even when auto-fuzzy is off.
fuzzy_enabled            = true
# Minimum post-stem length for auto-fuzzy. Stems shorter than this
# are exact-only. Default 3 matches the built-in AUTO heuristic;
# raise to 4/5 to suppress fuzzy on common short words.
fuzzy_min_term_chars     = 3      # 0-10

# A collection groups one or more source directories. The starter
# collection points at ~/Documents; edit, add more [[sources]] tables,
# or replace it entirely.
[[collections.default.sources]]
path = "~/Documents"
# includes = ["**/*.md", "**/*.pdf", "**/*.docx", "**/*.pptx", "**/*.txt"]
excludes = ["**/.git/**", "**/.DS_Store", "**/__pycache__/**"]
# follow_symlinks = false
# frontmatter_filter = "type == 'note'"  # md sources only — DSL described in docs.

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

# ── Apps & defaults ────────────────────────────────────────────────────
# Default app per filetype. Resolved in this order for the `o` shortcut:
#   1. per-source `app_for[kind]`           (set in [[collections.X.sources]])
#   2. per-source `app`                     (same place; sugar)
#   3. `[app_defaults][kind]`               (this section)
#   4. AUTO-DEFAULT for that kind           (see below)
#   5. `system` — `open <path>`             (LaunchServices, no page-jump)
#
# Auto-defaults only fire when nothing above sets a value:
#   pdf:  Skim if installed                 — silent skim:// URL, no permissions
#         else Preview if Accessibility
#         is granted to the launching app   — keystrokes Cmd-Opt-G in Preview;
#                                             dialog briefly flashes; needs
#                                             System Settings → Privacy &
#                                             Security → Accessibility for
#                                             your terminal / IDE
#         else system                       — opens at page 1
#   md, txt, docx, pptx: system             — no smart pick today
#
# Built-in app ids: system, preview, skim, pdf_expert, obsidian, vscode.
# Add your own under [apps.<id>] below.
# [app_defaults]
# pdf = "preview"   # or "skim" / "pdf_expert" / "system"
# md  = "obsidian"  # or "vscode" / "system"
# txt = "vscode"

# User-defined apps. See docs/apps/ for ready-to-paste configs for common
# third-party apps. Each entry sets exactly one of `argv` (process exec)
# or `url` (deep-link via `open <url>`). Template variables: {path},
# {path_pct}, {page}, {line}, {heading}, {heading_pct}, {query},
# {query_pct}, {vault}, {vault_pct}, {file_in_vault}, {file_in_vault_pct}.
# Empty fields render as the empty string; templates ending in `::N`
# (line missing, col set) collapse to just `{path}`.
#
# [apps.marked]
# display_name = "Marked 2"
# handles      = ["md"]
# argv         = ["open", "-a", "Marked 2", "{path}"]
#
# [apps.typora]
# display_name = "Typora"
# handles      = ["md"]
# argv         = ["open", "-a", "Typora", "{path}"]
"""

# Backwards-compat alias for any older callers that imported the
# previous name. Both refer to the same string.
STARTER_TEMPLATE = CONFIG_TEMPLATE
