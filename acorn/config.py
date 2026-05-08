"""Config + filesystem locations.

Per plan §6: a single TOML file owned by the user with collections, defaults,
and ranking profiles. acorn never silently rewrites it; commands like
``acorn collection add`` propose a diff and prompt before writing.

Phase 3 covers: defaults, collections (roots/includes/excludes/follow_symlinks).
Phase 7 adds ranking profiles (recency boost / filetype boost / phrase
proximity) — wired into :class:`acorn.rerank.RankingProfile` at search time.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

from platformdirs import user_data_dir
from pydantic import BaseModel, Field, field_validator

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


class CollectionConfig(BaseModel):
    """One named set of roots + filters."""

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


STARTER_TEMPLATE = """\
# acorn config — see plan §6 for the full schema.
# Edit with `acorn config edit`.

[defaults]
collection    = "default"
result_limit  = 200

[collections.default]
roots    = ["~/Documents"]
# includes = ["**/*.pdf", "**/*.docx", "**/*.pptx", "**/*.md", "**/*.txt"]
excludes = ["**/.git/**", "**/.DS_Store", "**/__pycache__/**"]
"""
