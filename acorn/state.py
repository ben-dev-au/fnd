"""Persistent UI state — active scope, panel collapse, etc.

Stored in ``<app_data_dir>/state/scope.toml`` so it survives app
restarts. Atomic writes (temp file + rename) so a crash mid-write
can't leave the file half-empty.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

import tomlkit

from acorn.config import app_data_dir


def _state_path() -> Path:
    d = app_data_dir() / "state"
    d.mkdir(parents=True, exist_ok=True)
    return d / "scope.toml"


@dataclass(slots=True)
class UiState:
    """Snapshot of UI state we want to survive restarts."""

    collections: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    collapsed_panels: list[str] = field(default_factory=list)


def load(path: Path | None = None) -> UiState:
    """Read the on-disk state. Missing / unreadable files return an
    empty :class:`UiState` rather than raising — first launch is a
    valid state."""
    p = path if path is not None else _state_path()
    if not p.exists():
        return UiState()
    try:
        raw = tomllib.loads(p.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return UiState()
    scope_raw = raw.get("scope", {})
    panels_raw = raw.get("panels", {})
    scope = scope_raw if isinstance(scope_raw, dict) else {}
    panels = panels_raw if isinstance(panels_raw, dict) else {}
    return UiState(
        collections=[s for s in scope.get("collections", []) if isinstance(s, str)],
        sources=[s for s in scope.get("sources", []) if isinstance(s, str)],
        collapsed_panels=[s for s in panels.get("collapsed", []) if isinstance(s, str)],
    )


def save(state: UiState, path: Path | None = None) -> None:
    """Atomic write of the state TOML."""
    p = path if path is not None else _state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    doc = tomlkit.document()
    scope = tomlkit.table()
    scope["collections"] = list(state.collections)
    scope["sources"] = list(state.sources)
    doc["scope"] = scope
    panels = tomlkit.table()
    panels["collapsed"] = list(state.collapsed_panels)
    doc["panels"] = panels
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(tomlkit.dumps(doc), encoding="utf-8")
    os.replace(tmp, p)
