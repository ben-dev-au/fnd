"""Config + filesystem locations.

A single TOML file owned by the user, holding collections, defaults and
ranking profiles. ``fnd config edit`` is the read-modify-write entry point;
``fnd collection add`` appends a new ``[[sources]]`` table via ``tomlkit``,
preserving comments and unrelated tables.

Ranking profiles are wired into :class:`fnd.rerank.RankingProfile` at search
time. A collection holds :class:`SourceConfig` entries, each with its own
filter chain.
"""

from __future__ import annotations

import datetime as dt
import re
import sys
import tomllib
from collections.abc import Callable, Collection, Sequence
from pathlib import Path
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator, model_validator

from fnd.kinds import ALL_KIND_IDS, CATEGORY_IDS, KIND_SPECS, split_type_globs
from fnd.paths import app_data_dir  # re-exported: many modules import it from here

_APP_NAME = "fnd"


def default_index_dir() -> Path:
    from fnd._perms import secure_mkdir

    return secure_mkdir(app_data_dir() / "index")


def default_config_path() -> Path:
    """Resolve the config-file path with a fallback chain.

    Primary: ``$XDG_DATA_HOME/fnd/config.toml`` (or platformdirs equivalent
    on macOS, under Application Support). Fallback: ``~/.config/fnd/config.toml``.
    """
    primary = app_data_dir() / "config.toml"
    if primary.exists():
        return primary
    fallback = Path.home() / ".config" / _APP_NAME / "config.toml"
    if fallback.exists():
        return fallback
    return primary


# ── Schema ──────────────────────────────────────────────────────────────────

# Directory basenames pruned at walk descent. Matched by ``name`` only;
# any directory anywhere in the tree with one of these names is skipped
# entirely (no scandir into it). Comprehensive because the typical user
# is a developer with many code trees on disk and indexing build/cache
# output is never useful. Per-source ``excludes`` still apply on top;
# users can disable this list with ``defaults.skip_junk_dirs = false``
# or extend it via ``defaults.extra_junk_dirs``.
DEFAULT_JUNK_DIRS: frozenset[str] = frozenset(
    {
        # Version control
        ".git",
        ".hg",
        ".svn",
        ".bzr",
        "_darcs",
        "CVS",
        # Node / JavaScript / TypeScript
        "node_modules",
        "bower_components",
        "jspm_packages",
        ".next",
        ".nuxt",
        ".svelte-kit",
        ".astro",
        ".docusaurus",
        ".vitepress",
        ".vinxi",
        ".turbo",
        ".parcel-cache",
        ".rollup.cache",
        ".swc",
        ".yarn",
        ".pnpm-store",
        ".npm",
        ".angular",
        ".firebase",
        ".expo",
        ".expo-shared",
        ".vercel",
        ".netlify",
        # Python
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".pyre",
        ".pytype",
        ".tox",
        ".nox",
        ".eggs",
        ".hypothesis",
        "venv",
        ".venv",
        "env",
        ".virtualenv",
        "virtualenv",
        ".ipynb_checkpoints",
        # Ruby
        ".bundle",
        # Java / Kotlin / Gradle / Maven
        ".gradle",
        ".m2",
        # Swift / iOS / macOS dev
        "Pods",
        "DerivedData",
        "Carthage",
        ".swiftpm",
        "xcuserdata",
        # C / C++ / CMake
        "CMakeFiles",
        # Editors / IDEs
        ".idea",
        ".vs",
        ".vscode-test",
        ".fleet",
        ".history",
        # Infrastructure / DevOps
        ".terraform",
        ".terragrunt-cache",
        ".pulumi",
        ".serverless",
        # Tooling caches
        ".husky",
        ".changeset",
        ".sass-cache",
        ".cache",
        # Mobile
        ".dart_tool",
        ".metro-cache",
        # macOS Finder / system clutter
        ".Spotlight-V100",
        ".fseventsd",
        ".Trashes",
        ".DocumentRevisions-V100",
        ".TemporaryItems",
        ".AppleDouble",
        # Windows system clutter
        "$RECYCLE.BIN",
        "System Volume Information",
    }
)


# Indexer-supported file types, keyed by fine-grained kind id → display label,
# derived from the central registry (fnd.kinds). Used by the Add Source / Add
# Collection wizards' Includes multi-select. The wizard groups these by
# category and expands a selected kind to globs for all its suffixes.
INDEXER_FILETYPES: dict[str, str] = {
    spec.id: f"{spec.label} ({'/'.join(spec.suffixes)})" for spec in KIND_SPECS
}

# Exclude presets for the Add Collection wizard's Excludes multi-select. Each
# preset defines a set of globs and a default toggle state. Presets marked
# default=True are pre-ticked in the UI.
EXCLUDES_PRESETS: dict[str, dict[str, Any]] = {
    # Hidden names are pruned by the walk whatever this says, so the label
    # promised something unticking it cannot give back. What it uniquely adds
    # is desktop.ini, plus hidden paths an include glob deliberately admitted.
    "hidden": {
        "label": "System files (hidden are always skipped)",
        "globs": ["**/.*", "**/.DS_Store", "**/Thumbs.db", "**/desktop.ini", "**/.git/**"],
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


# Reserved pseudo-name meaning "every configured collection". Accepted by
# ``--collection``/``-c`` and stored in ``defaults.collection``. Matched
# case-insensitively so ``-c All`` works as readily as ``-c all``.
ALL_COLLECTIONS: Final = "all"

# 200 cost 3-4x the latency of 50 on a 110k-document index (202ms -> 1032ms):
# the chunk pool is `limit * 10`, so the display cap sizes the tantivy fetch.
DEFAULT_RESULT_LIMIT: Final = 50


def is_all_collections(value: str | None, *, known: Collection[str] = ()) -> bool:
    """Whether ``value`` is the all-collections pseudo-name.

    A real collection literally named ``all`` wins, so configs written
    before the name was reserved keep resolving to their own collection
    rather than silently widening to everything.
    """
    if not value or value in known:
        return False
    return value.strip().casefold() == ALL_COLLECTIONS


class _ConfigModel(BaseModel):
    """Base for every config model.

    Attribute docstrings become the field descriptions the canonical renderer
    prints, so a key cannot reach a user's config file undocumented and the
    prose cannot drift from the schema.
    """

    model_config = ConfigDict(use_attribute_docstrings=True, extra="forbid")


def _known_kinds(values: list[str] | None) -> list[str] | None:
    """Reject a file type that does not exist.

    An unknown kind is not inert: `kinds` means "only these", so a misspelt
    one indexes nothing at all, silently. The plausible guesses (`markdown`,
    `python`) are the wrong ids, so name the nearest real one.
    """
    import difflib

    from fnd.kinds import ALL_KIND_IDS, KIND_BY_ID

    # A wrong guess is usually the type's NAME or its extension rather than a
    # misspelling of the id (`markdown` for `md`, `.py` for `python`), so look
    # those up before falling back to fuzzy matching.
    by_word = {
        word.lower().lstrip("."): kind
        for kind, spec in KIND_BY_ID.items()
        for word in (spec.label, *spec.suffixes)
    }
    for value in values or ():
        if value in ALL_KIND_IDS:
            continue
        named = by_word.get(value.lower().lstrip("."))
        near = named or next(
            iter(difflib.get_close_matches(value, sorted(ALL_KIND_IDS), n=1, cutoff=0.6)), None
        )
        hint = f"; did you mean {near!r}?" if near else ""
        raise ValueError(f"no such file type {value!r}{hint}")
    return values


class DefaultFilters(_ConfigModel):
    """``[defaults.filters]``; every field concrete, so it can be the base a
    per-source override resolves against."""

    model_config = ConfigDict(extra="forbid")

    respect_gitignore: bool = True
    """Honour .gitignore files found in the tree. A file git ignores is not
    indexed; the next update prunes any already in the index."""

    respect_fndignore: bool = True
    """Honour .fndignore files. Same syntax as .gitignore, but they apply to fnd
    alone, so they exclude something from search without excluding it from git."""

    include_tags: list[str] | dict[str, list[str]] = Field(default_factory=list)
    """Index only files carrying one of these tags. Empty means no tag is needed."""

    exclude_tags: list[str] | dict[str, list[str]] = Field(default_factory=lambda: ["no_index"])
    """Skip files carrying any of these tags. Reads OS file tags and a note's YAML
    `tags:`. A folder's own tag is not inherited."""

    kinds: list[str] = Field(default_factory=list)
    """Restrict to these file types, e.g. ["md", "pdf"]. Empty means every
    supported type."""

    min_size: int | None = None
    """Skip files smaller than this many bytes. Unset means no floor."""

    max_size: int | None = None
    """Skip files larger than this many bytes. Unset means no ceiling."""

    created_after: dt.date | None = None
    """Index only files created on or after this date. An absolute date, not a
    rolling window."""

    created_before: dt.date | None = None
    """Index only files created on or before this date."""

    modified_after: dt.date | None = None
    """Index only files last modified on or after this date."""

    modified_before: dt.date | None = None
    """Index only files last modified on or before this date."""

    frontmatter: str | None = None
    """Filter expression over a note's YAML frontmatter, e.g.
    `Course == 'Design Patterns'`. A note with no frontmatter block matches no
    field test."""

    expression: str | None = None
    """Filter expression over file facts: `file.name`, `file.ext`,
    `file.kind`, `file.path`, `file.size`. For anything the rows above cannot
    say, e.g. `file.name ~~ 'draft-*'`."""

    @field_validator("frontmatter", "expression")
    @classmethod
    def _validate_expression(cls, v: str | None) -> str | None:
        return _compiled_or_error(v, "filters")

    @field_validator("kinds")
    @classmethod
    def _validate_kinds(cls, v: list[str] | None) -> list[str] | None:
        return _known_kinds(v)


#: Fields with no value meaning "nothing". A list clears with `[]`, a string
#: with `""` and a bool with `false`; for these `None` already means inherit.
CLEARABLE: Final = frozenset(
    {"min_size", "max_size", "created_after", "created_before", "modified_after", "modified_before"}
)


class SourceFilters(_ConfigModel):
    """Per-source overrides. ``None`` inherits ``[defaults.filters]``; the two
    models are distinct types so "unset" is never confused with "set to the
    same value as the default"."""

    model_config = ConfigDict(extra="forbid")

    respect_gitignore: bool | None = None
    respect_fndignore: bool | None = None
    include_tags: list[str] | dict[str, list[str]] | None = None
    exclude_tags: list[str] | dict[str, list[str]] | None = None
    kinds: list[str] | None = None
    min_size: int | None = None
    max_size: int | None = None
    created_after: dt.date | None = None
    created_before: dt.date | None = None
    modified_after: dt.date | None = None
    modified_before: dt.date | None = None
    frontmatter: str | None = None
    expression: str | None = None

    clears: list[str] = Field(default_factory=list)
    """Numeric and date bounds dropped rather than inherited.

    TOML has no null and `None` means inherit, so clearing a bound means
    naming it.
    """

    @field_validator("clears")
    @classmethod
    def _clearable_fields(cls, names: list[str]) -> list[str]:
        wrong = sorted(set(names) - CLEARABLE)
        if wrong:
            raise ValueError(
                f"clears only applies to {', '.join(sorted(CLEARABLE))}; got {', '.join(wrong)}"
            )
        return names

    @field_validator("frontmatter", "expression")
    @classmethod
    def _validate_expression(cls, v: str | None) -> str | None:
        # Empty is kept, not folded to None: here it means "this source has no
        # expression", which differs from "inherit" whenever the defaults set
        # one. The defaults model normalises it away again on resolution.
        if v is not None and not v.strip():
            return ""
        return _compiled_or_error(v, "filters")

    @field_validator("kinds")
    @classmethod
    def _validate_kinds(cls, v: list[str] | None) -> list[str] | None:
        return _known_kinds(v)


def _compiled_or_error(value: str | None, label: str) -> str | None:
    """Compile eagerly so a syntax error surfaces at load with its column."""
    if value is None or not value.strip():
        return None
    from fnd.filter_dsl import FilterError, compile_filter

    try:
        compile_filter(value)
    except FilterError as e:
        raise ValueError(f"{label}: {e.message} (col {e.column})") from e
    return value


def resolve_filters(source: SourceFilters | None, defaults: DefaultFilters) -> DefaultFilters:
    """Per-field override; an unset field takes the default's value."""
    if source is None:
        return defaults
    merged = defaults.model_dump()
    for field_name, value in source.model_dump().items():
        if value is not None and field_name != "clears":
            merged[field_name] = value
    for field_name in source.clears:
        merged[field_name] = None
    return DefaultFilters.model_validate(merged)


class SourceConfig(_ConfigModel):
    """One root path inside a collection with its own filter chain.

    Optional ``app`` / ``app_for`` / ``app_params`` fields wire this
    source into the apps registry. See :mod:`fnd.apps` for resolution
    semantics. Validation of app id existence happens at the top-level
    :class:`Config` model_validator; sources can't see siblings.
    """

    path: Path
    """Folder to index. `~` is expanded; a relative path resolves against
    the working directory at load time."""

    includes: list[str] = Field(default_factory=list)
    """Glob patterns a file must match to be indexed. Empty means every
    supported type. Matched against the path relative to this source root."""

    excludes: list[str] = Field(default_factory=list)
    """Glob patterns that keep a file out, even when it matched an include."""

    follow_symlinks: bool = False
    """Follow symlinks in this source, and allow the root to be one. Off by
    default."""

    frontmatter_filter: str | None = Field(default=None, deprecated="use filters.frontmatter")
    """Legacy name for `filters.frontmatter`; still honoured, folded into the
    filters table when this source is next saved."""

    filters: SourceFilters | None = None
    """Index-time filters every source inherits. A source's own filters table
    overrides these per field."""

    app: str | None = None
    """When set, this app id is used for any of its declared ``handles``. Sugar
    for the common single-app case."""

    app_for: dict[str, str] = Field(default_factory=dict)
    """Per-filetype override: ``{"md": "obsidian", "pdf": "skim"}``. Wins against
    ``app`` per-kind."""

    app_params: dict[str, str] = Field(default_factory=dict)
    """Free-form template-variable bag, surfaced as ``{vault}`` etc. in apps
    registry templates. Common keys: ``vault`` (Obsidian vault name)."""

    _resolved_filters: DefaultFilters | None = PrivateAttr(default=None)
    """Populated by :class:`Config` once the defaults are known. Private so it
    never reaches the TOML writer, which must emit only what the user set."""

    @model_validator(mode="after")
    def _absorb_type_globs(self) -> SourceConfig:
        """``includes = ["**/*.md"]`` is ``filters.kinds = ["md"]`` said twice.

        Holding both lets the settings UI show one and the walk obey the
        other. Skipped when the source states ``kinds`` itself, so an explicit
        choice is never overwritten; idempotent, because the globs it moves
        are gone from ``includes`` afterwards.
        """
        if self.filters is not None and self.filters.kinds is not None:
            return self
        kinds, rest = split_type_globs(self.includes)
        # Only when the globs say nothing else: ``walk`` ORs include globs but
        # ANDs ``kinds`` with them, so absorbing half of ["**/*.md", "notes/**"]
        # would turn "md files or anything under notes/" into "md files under notes/".
        if not kinds or rest:
            return self
        merged = (
            self.filters.model_copy(update={"kinds": kinds})
            if self.filters is not None
            else SourceFilters(kinds=kinds)
        )
        object.__setattr__(self, "filters", merged)
        object.__setattr__(self, "includes", rest)
        return self

    @property
    def legacy_frontmatter(self) -> str | None:
        """The pre-migration rule, read without tripping the deprecation.

        A config is migrated on disk at startup, not at load, so a rule still
        living here has to be readable. Attribute access warns once per read,
        which a walk makes once per source and a settings screen once per
        repaint.
        """
        return self.__dict__.get("frontmatter_filter")

    @property
    def effective_filters(self) -> DefaultFilters:
        """Overrides merged over the defaults.

        Falls back to the shipped defaults for a source built outside a
        :class:`Config`, so an ad-hoc walk filters the same way a configured
        one does.
        """
        if self._resolved_filters is not None:
            return self._resolved_filters
        return resolve_filters(self.filters, DefaultFilters())

    @field_validator("path", mode="before")
    @classmethod
    def _expand_path(cls, v: object) -> object:
        return Path(str(v)).expanduser()

    @field_validator("frontmatter_filter")
    @classmethod
    def _validate_filter(cls, v: str | None) -> str | None:
        # Eagerly compile so a syntax error surfaces at config load with
        # the parser's column. The compiled predicate is rebuilt on demand
        # at index time; caching here would couple the model to runtime.
        if v is None or not v.strip():
            return None
        from fnd.filter_dsl import FilterError, compile_filter

        try:
            compile_filter(v)
        except FilterError as e:
            raise ValueError(f"frontmatter_filter: {e.message} (col {e.column})") from e
        return v


def overlapping_source(
    sources: Sequence[Any], new: Any, editing: int | None = None
) -> tuple[str, bool]:
    """``(sibling path, whether the NEW source contains it)``, or ``("", False)``.

    The relation comes back because the check runs both ways and the warning
    has to name the one that matched: a caller told only the path would say
    "already inside" when the new folder is the parent. Not a refusal: the
    index keys on the file, so a file two sources reach is stored once; it is
    silence that misleads. ``editing`` is the row being replaced, not its own rival.
    """
    import contextlib

    with contextlib.suppress(Exception):
        target = Path(new.path).expanduser().resolve()
        for i, other in enumerate(sources):
            if i == editing or not other.path:
                continue
            path = Path(other.path).expanduser().resolve()
            if target == path or target.is_relative_to(path):
                return str(other.path), False
            if path.is_relative_to(target):
                return str(other.path), True
    return "", False


DEFAULT_RANKING_PROFILE = "default"
"""The profile a collection scores with until one is named. Not every config
defines a `[ranking.*]` block, so the picker has to offer this one itself."""


class CollectionConfig(_ConfigModel):
    """One named set of sources + collection-wide options.

    A collection can be configured in two equivalent shapes:

    * **New (recommended):** ``[[collections.X.sources]]``. One TOML table
      per source, each with its own includes/excludes/frontmatter_filter.
    * **Legacy:** flat ``roots = [...]``, ``includes = [...]``,
      ``excludes = [...]`` on the collection. Loader normalises this into
      a single implicit source so downstream code only sees the new shape.

    Mixing both forms on the same collection is rejected at load.
    """

    sources: list[SourceConfig] = Field(default_factory=list)
    """The current shape. One table per source folder."""

    roots: list[Path] = Field(default_factory=list)
    """Legacy flat shape. Still accepted, reconciled into sources below."""

    includes: list[str] = Field(default_factory=list)
    """Applied to every source in this collection that sets none of its own."""

    excludes: list[str] = Field(default_factory=list)
    """Applied to every source in this collection that sets none of its own."""

    follow_symlinks: bool = False
    """Follow symlinks in this source, and allow the root to be one. Off by
    default."""

    ranking_profile: str = DEFAULT_RANKING_PROFILE
    """Name of the `[ranking.*]` profile this collection scores with."""

    @field_validator("roots", mode="before")
    @classmethod
    def _expand_roots(cls, v: object) -> object:
        if not isinstance(v, list):
            return v
        return [Path(str(p)).expanduser() for p in v]

    @model_validator(mode="after")
    def _normalise_sources(self) -> CollectionConfig:
        # sources were explicitly provided alongside roots; only valid if roots
        # is already empty (idempotent re-validation path is handled below).
        if self.sources and self.roots:
            # Check whether this is an already-normalised model being re-validated
            # (e.g. when a CollectionConfig instance is nested inside Config()).
            # In that case sources already reflect the promoted roots, so roots
            # is stale; just clear it.
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


class RankingProfileConfig(_ConfigModel):
    """User-facing knobs that map onto :class:`fnd.rerank.RankingProfile`.

    ``bm25_k1`` / ``bm25_b`` are accepted for forward-compat and silently
    ignored at runtime: tantivy hardcodes them upstream.
    """

    recency_boost: float = 0.0
    """How much a recent modification time lifts a result. 0 ignores mtime
    entirely; higher values favour recent files more strongly."""

    recency_half_life: str = "365d"  # parsed via _parse_duration
    """Age at which the recency boost has decayed by half, e.g. "365d", "12w"."""

    filetype_boosts: dict[str, float] = Field(default_factory=dict)
    """Per-type score multipliers, e.g. { md = 1.0, pdf = 0.85 }. A type not
    named here scores at 1.0."""

    phrase_proximity: float = 0.0
    """Extra proximity boost applied after ranking. 0 disables it."""

    proximity_max_window: int = 50
    """Word distance within which two query terms count as near each other
    for the proximity boost."""

    bm25_k1: float | None = None
    """BM25 term-frequency saturation. Accepted for forward compatibility
    and currently ignored, as tantivy hardcodes it."""

    bm25_b: float | None = None
    """BM25 length normalisation. Accepted for forward compatibility and
    currently ignored, as tantivy hardcodes it."""


class AppConfig(_ConfigModel):
    """User-extensible app entry from ``[apps.<id>]``.

    Exactly one of ``argv`` (process exec) and ``url`` (deep-link via
    ``open <url>``) must be set. Template variables use ``{name}``-style
    placeholders; see ``docs/apps.md`` for the full variable list.
    Built-in apps (``system``, ``preview``, ``skim``, ``pdf_expert``,
    ``obsidian``, ``vscode``) ship in :mod:`fnd.apps` and do NOT appear
    here unless the user is overriding them.
    """

    display_name: str
    """Name shown for this app in the Settings menu."""

    handles: list[str]
    """File types this app opens, e.g. ["md"]. Used to resolve the Open
    shortcut when a source or default names this app."""

    argv: list[str] | None = None
    """Command line to launch, as a list. Supports template variables such
    as {path} and {page}. Omit to open via the URL scheme instead."""

    url: str | None = None
    """URL scheme template to open instead of a command line, e.g. an
    `obsidian://` link. One of argv or url must be set."""

    notes: str = ""
    """Free-text note about this app. Shown in Settings, ignored otherwise."""

    @model_validator(mode="after")
    def _argv_xor_url(self) -> AppConfig:
        if (self.argv is None) == (self.url is None):
            raise ValueError("AppConfig: exactly one of argv or url must be set")
        return self

    @field_validator("handles")
    @classmethod
    def _validate_handles(cls, v: list[str]) -> list[str]:
        # Same allow-list as fnd.apps.ALLOWED_HANDLES, derived from the central
        # registry here rather than importing apps.py at config-load time
        # (apps.py imports opener which imports config; the dependency stays
        # one-way; fnd.kinds imports nothing from fnd so it's cycle-safe).
        allowed = set(ALL_KIND_IDS) | set(CATEGORY_IDS) | {"*", "markdown"}
        for h in v:
            if h not in allowed:
                raise ValueError(f"unknown handle kind {h!r}; allowed: {sorted(allowed)}")
        if not v:
            raise ValueError("handles must be non-empty")
        return v


class Defaults(_ConfigModel):
    collection: str = ALL_COLLECTIONS
    """Collection searched when `--collection` is omitted; "all" means every one.
    Seeds scope only until you tick collections in the sidebar."""

    tag_sources: list[Literal["frontmatter", "os"]] = ["frontmatter", "os"]
    """Which tag sources feed the Tags filter. Removing one applies at once;
    re-adding one needs a Rebuild index, since tags are read at index time."""

    tag_frontmatter_keys: list[str] = []
    """Extra frontmatter keys treated as tags, e.g. `Course:`. Values namespace
    under the key (course/algebra). Needs a Rebuild index; tags are read at
    index time."""

    result_limit: int = DEFAULT_RESULT_LIMIT
    """How many result rows a search returns. Lower it to speed up a slow query."""

    preview_chunks: int = 5
    """Chunks of a file shown in the preview pane around the match."""

    debounce_ms: int = 200
    """Idle delay after typing before the search runs, in milliseconds.
    0 searches on every keystroke."""

    drill_summary_mode: Literal["always_show", "smart", "always_ellipsis"] = "always_show"
    """Trailing summaries on Settings rows: always_show, smart (only rows with
    content), or always_ellipsis."""

    sections_score_threshold: float = 0.5
    """Keep a section when its score is at least this fraction of the file's best.
    1.0 keeps only the top one."""

    sections_per_file_max: int = 200
    """Hard cap on sections kept per file, whatever the score threshold
    admits. A safety net against one huge file filling the results."""

    preview_decode_workers: int = 4
    """Threads used to decode preview chunks. 1 is serial; raise for very large
    PDFs."""

    preview_warm_margin: int = 2
    """Chunks warmed either side of a match, so a jump lands with context ready. 0
    warms matches only."""

    preview_load_debounce_ms: int = 150
    """Idle delay before a cursor move loads a preview, in milliseconds. 0 loads
    instantly."""

    preview_prefetch_count: int = 4
    """Top result files decoded in the background after a search, so opening them
    is instant. 0 disables it."""

    fuzzy_enabled: bool = True
    """Auto-fuzzy matching in the cascade fallback. When False, only per-term
    ``~N`` modifiers in the query trigger fuzzy expansion."""

    fuzzy_min_term_chars: int = 3
    """Shortest stem auto-fuzzy applies to. Shorter terms stay exact."""

    indexer_auto_resume: bool = False
    """Resume an interrupted reindex on launch. Off by default, so indexing never
    starts unasked."""

    cache_at_index_time: bool = True
    """Write PDF structure cache entries while indexing. Off reads the cache but
    adds nothing, for a faster refresh."""

    cloud_fetch_timeout_s: int = 60
    """Seconds to wait for a cloud-only file to download before skipping it. Raise
    it on a slow link."""

    scrollbar_match_highlight: bool = False
    """IN DEVELOPMENT: mark match positions on the preview scrollbar. Markers
    drift on large markdown."""

    multicolour_highlights: bool = True
    """Paint each query term in its own colour. Off uses one yellow for all of
    them."""

    preview_scroll_animation: bool = True
    """Glide to a match inside the file already shown. Off cuts straight to it."""

    render_mermaid: bool = True
    """IN DEVELOPMENT: render `mermaid` fences as terminal diagrams, falling back
    to source when it cannot."""

    skip_junk_dirs: bool = True
    """Skip well-known dev and cache directories (node_modules, __pycache__,
    .venv) while walking."""

    extra_junk_dirs: list[str] = Field(default_factory=list)
    """Additional directory basenames to prune at walk descent on top of
    :data:`DEFAULT_JUNK_DIRS`. Matched by basename only; e.g. ``["build",
    "dist"]`` skips any ``build/`` or ``dist/`` subtree."""

    filters: DefaultFilters = Field(default_factory=DefaultFilters)
    """Index-time filters every source inherits. A source's own filters table
    overrides these per field."""


class Config(_ConfigModel):
    defaults: Defaults = Field(default_factory=Defaults)
    """App-wide settings and the filters every source inherits."""

    collections: dict[str, CollectionConfig] = Field(default_factory=dict)
    """Named groups of source folders, searched together."""

    ranking: dict[str, RankingProfileConfig] = Field(default_factory=dict)
    """Named ranking profiles a collection can point at."""

    apps: dict[str, AppConfig] = Field(default_factory=dict)
    """App registry. Keys are app ids used by `app_defaults` and a source's `app`
    field."""

    app_defaults: dict[str, str] = Field(default_factory=dict)
    """Default app per file type, e.g. `md = "obsidian"`. Missing types use the
    system default."""

    @model_validator(mode="after")
    def _resolve_source_filters(self) -> Config:
        """Fold ``[defaults.filters]`` into every source once, here.

        Doing it at load keeps ``walk_sources(sources=…)`` a one-argument call,
        so the eight callers that never learned about defaults; the PDF
        inventories, cost estimates and texture maintenance among them; stay
        in step with the indexer by construction.
        """
        for collection in self.collections.values():
            for source in collection.sources:
                source._resolved_filters = resolve_filters(source.filters, self.defaults.filters)
        return self

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
        # Per-source app references; same id-existence rule applies.
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
        when ``name`` is missing; callers can opt out of ranking just by
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
# read side; it's a freeform shorthand and any non-matching name
# just yields no results from Tantivy. Keeping the DSL permissive
# means a typo'd `c:` token doesn't silently get dropped from the
# query.
_COLLECTION_NAME_MAX = 64
# Characters that would break a downstream consumer of the name. ``/`` and
# ``\`` corrupt the per-collection state file path; quotes collide with
# TOML / shell / DSL quoting; backtick is a shell metacharacter; ``,`` is
# the ``c:<a>,<b>`` DSL list separator (a name containing ``,`` would be
# ambiguous in the bare form); control characters and DEL are never
# useful. Spaces are deliberately allowed; users want collection names
# like "Soft Eng Textbooks", and the DSL parser supports the quoted form
# ``c:"name with spaces"`` to reference them.
_COLLECTION_NAME_FORBIDDEN: frozenset[str] = (
    frozenset({"/", "\\", '"', "'", "`", ","})
    | frozenset(chr(c) for c in range(0x20))
    | frozenset({chr(0x7F)})
)

# Windows reserved device names; unusable as a filename stem, so a collection
# so named can't own a ``<name>.state.toml``. Checked case-insensitively.
_WIN_RESERVED_NAMES: frozenset[str] = (
    frozenset({"CON", "PRN", "AUX", "NUL"})
    | frozenset(f"COM{i}" for i in range(1, 10))
    | frozenset(f"LPT{i}" for i in range(1, 10))
)


class InvalidCollectionNameError(ValueError):
    """Raised when a collection name fails :func:`validate_collection_name`."""


def validate_collection_name(name: str) -> str:
    """Return ``name`` if it's safe for every downstream use, else raise.

    Rules (each enforced with its own error message so the wizard / CLI
    can surface a specific reason rather than a regex dump):

    - 1 to ``_COLLECTION_NAME_MAX`` (64) characters
    - first character is an ASCII letter, digit, or underscore; keeps
      the name away from shell-flag (``-``) and hidden-file (``.``)
      collisions, and avoids leading whitespace
    - no trailing whitespace or dot; those are filesystem footguns on
      macOS/Windows
    - no path separators, quote characters, backticks, or control chars

    Names round-trip through TOML (tomlkit auto-quotes non-bare keys),
    the CLI ``-c "name"`` flag, the ``c:"name"`` DSL shorthand, and the
    per-collection state file path.
    """
    if not name:
        raise InvalidCollectionNameError("collection name must not be empty")
    if len(name) > _COLLECTION_NAME_MAX:
        raise InvalidCollectionNameError(
            f"collection name {name!r} exceeds {_COLLECTION_NAME_MAX} characters"
        )
    first = name[0]
    if not (first.isascii() and (first.isalnum() or first == "_")):
        raise InvalidCollectionNameError(
            f"collection name {name!r} must start with an ASCII letter, digit, or underscore "
            "(not a space, dot, hyphen, or symbol)"
        )
    if name[-1] in " .":
        raise InvalidCollectionNameError(
            f"collection name {name!r} must not end with whitespace or a dot"
        )
    bad = _COLLECTION_NAME_FORBIDDEN & set(name)
    if bad:
        shown = ", ".join(sorted(repr(c) for c in bad))
        raise InvalidCollectionNameError(
            f"collection name {name!r} contains forbidden character(s): {shown}"
        )
    # Windows reserves device names (CON, PRN, …) for ANY file whose stem (the
    # part before the first dot) matches, so ``CON`` → ``CON.state.toml``
    # can't be created. Reject only on Windows so an existing macOS/Linux config
    # with such a name keeps loading; it fails clearly if that config is opened
    # on Windows rather than dying on an opaque file-creation error.
    if sys.platform == "win32" and name.split(".", 1)[0].upper() in _WIN_RESERVED_NAMES:
        raise InvalidCollectionNameError(
            f"collection name {name!r} is a reserved device name on Windows"
        )
    # ``all`` is the pseudo-name for "every collection" in ``-c`` and in
    # ``defaults.collection``; a real one would make those ambiguous. Only
    # blocked at write time, so an older config that already has one loads.
    if name.casefold() == ALL_COLLECTIONS:
        raise InvalidCollectionNameError(
            f"collection name {name!r} is reserved; it means 'every collection' in "
            "`--collection` and `defaults.collection`"
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
    Config with no collections; the caller decides whether to error.

    A config from a newer fnd is refused. Migrations are **not** applied here:
    a read must not have a side effect, and it must report an error against the
    key the file actually contains. :func:`ensure_current` migrates.
    """
    from fnd.config_migrations import check_version

    p = path if path is not None else default_config_path()
    if not p.exists():
        return Config()
    raw = tomllib.loads(p.read_text(encoding="utf-8"))
    check_version(raw)
    return Config.model_validate(raw)


def ensure_current(config_path: Path | None = None) -> list[str]:
    """Rewrite the config in the current shape, keeping a timestamped backup.

    Returns what was applied, empty when the file was already current. Called
    once at startup so every config converges on one syntax rather than each
    legacy shape being honoured forever.
    """
    from fnd._perms import secure_write_text
    from fnd.config_migrations import migrate
    from fnd.config_render import extract_preserved, render_config

    p = config_path if config_path is not None else default_config_path()
    if not p.exists():
        return []
    text = p.read_text(encoding="utf-8")
    raw = tomllib.loads(text)
    version, applied = migrate(raw)
    config = Config.model_validate(raw)
    rendered = render_config(config, preserved=extract_preserved(text), version=version)
    if rendered == text:
        return []
    _refuse_lossy(rendered, config, raw)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    # The backup carries the same source paths and filter expressions as the
    # live file, so it gets the same 0600, not whatever the umask allows.
    secure_write_text(p.with_name(f"{p.name}.bak-{stamp}"), text)
    secure_write_text(p, rendered, atomic=True)
    return applied or ["Adopt the canonical layout"]


def starter_config() -> str:
    """The file a fresh install gets: the canonical rendering of the defaults
    with one example collection. Generated, so it cannot drift from the models.
    """
    from fnd.config_render import render_config

    config = Config(
        collections={"default": CollectionConfig(sources=[SourceConfig(path=Path("~/Documents"))])}
    )
    return render_config(config)


def _rewrite(config_path: Path, mutate: Callable[[dict[str, Any]], None]) -> Config:
    """Read, edit the raw mapping, validate, then write the canonical form.

    Every writer goes through here, so what is on disk is always exactly what
    this build renders; which is what lets "is this config current?" be a
    byte comparison rather than an inspection of its keys.
    """
    from fnd._perms import secure_mkdir, secure_write_text
    from fnd.config_migrations import migrate
    from fnd.config_render import extract_preserved, render_config

    text = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    raw: dict[str, Any] = tomllib.loads(text) if text else {}
    version, _applied = migrate(raw)
    mutate(raw)
    config = Config.model_validate(raw)
    rendered = render_config(config, preserved=extract_preserved(text), version=version)
    _refuse_lossy(rendered, config, raw)
    secure_mkdir(config_path.parent)
    secure_write_text(
        config_path,
        rendered,
        atomic=True,
    )
    return config


def _refuse_lossy(rendered: str, expected: Config, source: dict[str, Any] | None = None) -> None:
    """Refuse to publish output that does not read back as what we rendered.

    Every writer replaces the whole file, so a value the renderer cannot
    express would be silently dropped or the file left unparseable. Failing
    the save keeps the previous config intact.
    """
    from fnd.config_migrations import check_version

    try:
        raw = tomllib.loads(rendered)
    except tomllib.TOMLDecodeError as e:
        raise ValueError(f"refusing to write a config that does not parse: {e}") from e
    check_version(raw)
    reloaded = Config.model_validate(raw)
    if reloaded != expected:
        differing = [
            name
            for name in Config.model_fields
            if getattr(reloaded, name) != getattr(expected, name)
        ]
        raise ValueError(f"refusing to write a config that does not round-trip: {differing}")
    if source is not None:
        # Only a table that held something: an emptied `[collections]` renders
        # as nothing, and deleting the last collection is a legitimate write.
        dropped = sorted(k for k in set(source) - set(raw) if source[k])
        if dropped:
            # A backstop, not the primary guard: `extra="forbid"` refuses an
            # unknown key at load, so what this can still catch is a *declared*
            # table the renderer failed to emit.
            raise ValueError(f"refusing to write a config that drops: {', '.join(dropped)}")


def _source_mapping(source: SourceConfig) -> dict[str, Any]:
    """A source as raw TOML data. Single point owning the field mapping, so
    every writer round-trips ``app`` / ``app_for`` / ``app_params`` alike."""
    return source.model_dump(exclude_none=True)


def write_collection_source(
    *,
    config_path: Path,
    collection_name: str,
    source: SourceConfig,
) -> None:
    """Append ``source`` to ``collection_name``, creating the file and the
    collection table if needed.

    Raises FileNotFoundError if the parent dir is missing; caller is
    expected to mkdir the config dir.
    """
    validate_collection_name(collection_name)

    def mutate(raw: dict[str, Any]) -> None:
        collections = raw.setdefault("collections", {})
        collection = collections.setdefault(collection_name, {})
        collection.setdefault("sources", []).append(_source_mapping(source))

    _rewrite(config_path, mutate)


def write_collection(
    *,
    config_path: Path,
    name: str,
    collection: CollectionConfig,
) -> None:
    """Replace ``[collections.<name>]`` and its sources.

    The supplied :class:`CollectionConfig` is the canonical post-validation
    form; this writer emits the ``[[sources]]`` shape and never the legacy
    flat ``roots = [...]`` one.
    """
    validate_collection_name(name)

    def mutate(raw: dict[str, Any]) -> None:
        data = collection.model_dump(exclude_none=True)
        data.pop("roots", None)
        raw.setdefault("collections", {})[name] = data

    _rewrite(config_path, mutate)


def _apply_setting(raw: dict[str, Any], dotted_path: str, value: object) -> None:
    """One dotted-path edit against the raw mapping."""
    parts = [p for p in dotted_path.split(".") if p]
    if not parts:
        raise ValueError("dotted_path must contain at least one segment")
    *parents, leaf = parts
    cursor = raw
    for part in parents:
        existing = cursor.get(part)
        if not isinstance(existing, dict):
            existing = {}
            cursor[part] = existing
        cursor = existing
    if value is None:
        # TOML has no null, so an emptied optional setting is the key's absence.
        cursor.pop(leaf, None)
    else:
        cursor[leaf] = value


def config_fingerprint(config_path: Path) -> str:
    """What the config file looked like, for spotting a change underneath.

    An editor holds the values it read when it opened. `_rewrite` re-reading
    from disk does not save it: the values being written are the stale ones,
    so a second editor's save is silently reverted by the first one's. Compare
    this before writing and the overwrite becomes a refusal.

    Bytes, not mtime: a second-granularity clock cannot separate two saves a
    keystroke apart, which is exactly the case that loses work.
    """
    import hashlib

    try:
        return hashlib.sha256(config_path.read_bytes()).hexdigest()
    except OSError:
        return ""


class ConfigChangedError(RuntimeError):
    """Raised when the file moved on since the editor read it."""


def write_settings(*, config_path: Path, values: dict[str, object]) -> Config:
    """Apply several dotted-path settings in one read-modify-write.

    One filter set is thirteen keys. Writing them one at a time meant a
    failure partway through; a read-only directory, a full disk; left a
    config that was neither the old set nor the new one.
    """

    def mutate(raw: dict[str, Any]) -> None:
        for dotted_path, value in values.items():
            _apply_setting(raw, dotted_path, value)

    return _rewrite(config_path, mutate)


def write_setting(*, config_path: Path, dotted_path: str, value: object) -> Config:
    """Update a single field in the config by dotted path.

    Examples of ``dotted_path``:

    - ``defaults.result_limit``
    - ``ranking.default.recency_boost``
    - ``collections.default.ranking_profile``

    A ``value`` of ``None`` removes the key: TOML has no null, so clearing an
    optional setting means the key is absent. On validation failure the
    on-disk file is not modified and the exception propagates.
    """
    return write_settings(config_path=config_path, values={dotted_path: value})


def clone_source(
    *,
    config_path: Path,
    source_collection: str,
    source_index: int,
    target_collection: str,
) -> int:
    """Deep-copy a source from one collection to another.

    Returns the new index in the target. Raises if either collection is
    missing or the index is out of range. The clone is independent; edits to
    the new entry do not propagate back to the original.
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
    cloned = SourceConfig.model_validate(sources[source_index].model_dump())
    write_collection_source(
        config_path=config_path,
        collection_name=target_collection,
        source=cloned,
    )
    return len(cfg.collections[target_collection].sources)


def delete_collection(*, config_path: Path, name: str, renamed_to: str | None = None) -> bool:
    """Remove ``[collections.<name>]`` and its sources. Idempotent: a no-op if
    the collection, or the file, is absent.

    Returns True when ``defaults.collection`` named it and had to move, to
    ``renamed_to`` on a rename and otherwise back to every collection. Left
    dangling it seeds no scope at all, so the saved default silently became
    "search everything" and only ``config validate`` said so.
    """
    if not config_path.exists():
        return False
    raw = tomllib.loads(config_path.read_text(encoding="utf-8"))
    collections = raw.get("collections")
    defaults = raw.get("defaults")
    stale_default = isinstance(defaults, dict) and defaults.get("collection") == name
    present = isinstance(collections, dict) and name in collections
    if not present and not stale_default:
        # Nothing to remove: writing anyway would materialise a whole config
        # where the caller asked for a no-op.
        return False

    def mutate(data: dict[str, Any]) -> None:
        tables = data.get("collections")
        if isinstance(tables, dict):
            tables.pop(name, None)
        table = data.get("defaults")
        if isinstance(table, dict) and table.get("collection") == name:
            table["collection"] = renamed_to or ALL_COLLECTIONS

    _rewrite(config_path, mutate)
    return stale_default
