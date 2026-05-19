"""Persistent UI state — active scope, panel collapse, etc.

Stored in ``<app_data_dir>/state/scope.toml`` so it survives app
restarts. Atomic writes (temp file + rename) so a crash mid-write
can't leave the file half-empty.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

import tomlkit

from fnd.config import app_data_dir


def _state_path() -> Path:
    from fnd._perms import secure_mkdir

    return secure_mkdir(app_data_dir() / "state") / "scope.toml"


@dataclass(slots=True)
class UiState:
    """Snapshot of UI state we want to survive restarts."""

    collections: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    collapsed_panels: list[str] = field(default_factory=list)
    # Per-section expand state within the secondary sidebar. Whole-panel
    # collapse lives in ``collapsed_panels``; these track the inner
    # branches/parent rows the user opened. Restored on launch so the
    # sidebar looks the way it did at quit.
    expanded_collections: list[str] = field(default_factory=list)
    expanded_filter_branches: list[str] = field(default_factory=list)
    # Phase F filters. Empty kinds list = "all kinds"; ``filter_date`` of
    # ``"any"`` = "any date". Anything else is treated as a literal token
    # for the DSL pre-pass (kind:pdf, mtime:week, …).
    #
    # Fuzzy/synonym/etc. are NOT filters — they're cascade passes that
    # broaden a sparse-result query automatically (§9c). The filters
    # panel only carries scope-narrowing knobs.
    filter_kinds: list[str] = field(default_factory=list)
    filter_date: str = "any"


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
    filters_raw = raw.get("filters", {})
    scope = scope_raw if isinstance(scope_raw, dict) else {}
    panels = panels_raw if isinstance(panels_raw, dict) else {}
    filters = filters_raw if isinstance(filters_raw, dict) else {}
    raw_date = filters.get("date", "any")
    filter_date = raw_date if isinstance(raw_date, str) else "any"
    return UiState(
        collections=[s for s in scope.get("collections", []) if isinstance(s, str)],
        sources=[s for s in scope.get("sources", []) if isinstance(s, str)],
        collapsed_panels=[s for s in panels.get("collapsed", []) if isinstance(s, str)],
        expanded_collections=[
            s for s in panels.get("expanded_collections", []) if isinstance(s, str)
        ],
        expanded_filter_branches=[
            s for s in panels.get("expanded_filter_branches", []) if isinstance(s, str)
        ],
        filter_kinds=[s for s in filters.get("kinds", []) if isinstance(s, str)],
        filter_date=filter_date,
    )


def save(state: UiState, path: Path | None = None) -> None:
    """Atomic write of the state TOML."""
    from fnd._perms import secure_mkdir, secure_write_text

    p = path if path is not None else _state_path()
    secure_mkdir(p.parent)
    doc = tomlkit.document()
    scope = tomlkit.table()
    scope["collections"] = list(state.collections)
    scope["sources"] = list(state.sources)
    doc["scope"] = scope
    panels = tomlkit.table()
    panels["collapsed"] = list(state.collapsed_panels)
    panels["expanded_collections"] = list(state.expanded_collections)
    panels["expanded_filter_branches"] = list(state.expanded_filter_branches)
    doc["panels"] = panels
    filters = tomlkit.table()
    filters["kinds"] = list(state.filter_kinds)
    filters["date"] = state.filter_date
    doc["filters"] = filters
    secure_write_text(p, tomlkit.dumps(doc), atomic=True)
