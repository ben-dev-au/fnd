"""Config + filesystem locations.

Per plan §6: a single TOML file owned by the user with collections, defaults,
and (later) ranking profiles. acorn never silently rewrites it; commands like
``acorn collection add`` propose a diff and prompt before writing.

Phase 3 covers: defaults, collections (roots/includes/excludes/follow_symlinks).
Phase 7 adds ranking profiles to this schema.
"""

from __future__ import annotations

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

    @field_validator("roots", mode="before")
    @classmethod
    def _expand_roots(cls, v: object) -> object:
        if not isinstance(v, list):
            return v
        return [Path(str(p)).expanduser() for p in v]


class Defaults(BaseModel):
    collection: str = "default"
    result_limit: int = 200
    preview_chunks: int = 5
    debounce_ms: int = 200


class Config(BaseModel):
    defaults: Defaults = Field(default_factory=Defaults)
    collections: dict[str, CollectionConfig] = Field(default_factory=dict)

    def collection(self, name: str) -> CollectionConfig:
        try:
            return self.collections[name]
        except KeyError as e:
            raise KeyError(
                f"unknown collection {name!r}; defined: {sorted(self.collections)}"
            ) from e


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
