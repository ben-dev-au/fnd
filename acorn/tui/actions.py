"""Action registry — single source of truth for the TUI's behavior surface.

Per plan §5 + §7: every TUI action is declared here once, with its default
keybinding and the ``:command`` name. Footer hints, the help overlay, and the
keymap loader all read from this registry, so they can never drift out of
sync with the actual bindings.

User overrides live in
``~/Library/Application Support/acorn/keybindings.toml``::

    [normal]
    "/"           = "focus_query"
    "ctrl+enter"  = "open_default"

The loader merges defaults ⊕ user overrides (user wins).
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from acorn.config import app_data_dir


@dataclass(slots=True, frozen=True)
class Action:
    """One TUI action — keymap and help-overlay row."""

    id: str  # snake_case name; matches AcornApp.action_<id>
    description: str
    default_key: str | None = None  # None = no default key (palette-only)
    command: str | None = None  # default = id; override for nicer names

    @property
    def palette_command(self) -> str:
        return self.command or self.id


# Authoritative registry. Order is the order shown in the help overlay.
# Phase 6 covers the actions wired in phase 5; later phases extend this list.
REGISTRY: tuple[Action, ...] = (
    Action(
        id="focus_query",
        description="Focus the query input (start typing).",
        default_key="slash",
        command="search",
    ),
    Action(
        id="toggle_focus",
        description="Switch focus between query bar and results pane.",
        default_key="tab",
    ),
    Action(
        id="open_focused",
        description="Open the focused result at its locator (Skim for PDFs).",
        default_key="enter",
        command="open",
    ),
    Action(
        id="open_default",
        description="Open the focused result in the default app (no page jump).",
        default_key="o",
    ),
    Action(
        id="peek_focused",
        description="Quick Look the focused file (no deep-link).",
        default_key="space",
        command="peek",
    ),
    Action(
        id="show_help",
        description="Toggle help overlay.",
        default_key="question_mark",
        command="help",
    ),
    Action(
        id="open_command_palette",
        description="Open the command palette to run any action by name.",
        default_key="colon",
        command="palette",
    ),
    Action(
        id="open_collection_picker",
        description="Toggle the collection picker (multi-select scope).",
        default_key="c",
        command="collections",
    ),
    Action(
        id="quit",
        description="Quit acorn.",
        default_key="q",
    ),
)


@dataclass(slots=True)
class Keymap:
    """Resolved keymap: per-key → action id. Defaults ⊕ user overrides."""

    bindings: dict[str, str] = field(default_factory=dict)

    def for_action(self, action_id: str) -> str | None:
        for key, aid in self.bindings.items():
            if aid == action_id:
                return key
        return None


def keybindings_path() -> Path:
    return app_data_dir() / "keybindings.toml"


def _registry_index() -> dict[str, Action]:
    return {a.id: a for a in REGISTRY}


def _command_index() -> dict[str, Action]:
    return {a.palette_command: a for a in REGISTRY}


def load_keymap(path: Path | None = None) -> Keymap:
    """Load default + user-overridden keymap.

    User TOML format::

        [normal]
        "j"    = "results_next"
        "ctrl+enter" = "open_default"

    Unknown action ids in the user file are dropped silently (the validator
    surfaces them via :func:`validate_keymap`)."""
    bindings: dict[str, str] = {a.default_key: a.id for a in REGISTRY if a.default_key}

    p = path if path is not None else keybindings_path()
    if p.exists():
        raw = tomllib.loads(p.read_text(encoding="utf-8"))
        normal = raw.get("normal", {}) if "normal" in raw else {}
        if not isinstance(normal, dict):
            normal = {}
        valid_ids = set(_registry_index())
        for key, aid in normal.items():
            if isinstance(key, str) and isinstance(aid, str) and aid in valid_ids:
                bindings[key] = aid
    return Keymap(bindings=bindings)


def validate_keymap(path: Path | None = None) -> list[str]:
    """Return a list of warnings for the user's keymap (e.g. unknown actions)."""
    p = path if path is not None else keybindings_path()
    if not p.exists():
        return []
    warnings: list[str] = []
    raw = tomllib.loads(p.read_text(encoding="utf-8"))
    valid_ids = set(_registry_index())
    valid_cmds = set(_command_index())
    normal = raw.get("normal", {})
    if not isinstance(normal, dict):
        warnings.append("[normal] must be a table")
        return warnings
    for key, aid in normal.items():
        if not isinstance(aid, str):
            warnings.append(f"[normal][{key!r}] must map to a string action id")
            continue
        if aid not in valid_ids and aid not in valid_cmds:
            warnings.append(
                f"[normal][{key!r}] = {aid!r}: unknown action; see `acorn tui` then `:help`"
            )
    return warnings


def resolve_command(name: str) -> Action | None:
    """Map a command-palette name (or action id) to an :class:`Action`."""
    by_id = _registry_index()
    if name in by_id:
        return by_id[name]
    by_cmd = _command_index()
    return by_cmd.get(name)
