"""Action registry — single source of truth for the TUI's behavior surface.

Per plan §5 + §7: every TUI action is declared here once, with its default
keybinding and the ``:command`` name. Footer hints, the help overlay, and the
keymap loader all read from this registry, so they can never drift out of
sync with the actual bindings.

User overrides live in
``~/Library/Application Support/fnd/keybindings.toml``::

    [normal]
    "/"           = "focus_query"
    "ctrl+enter"  = "open_default"

The loader merges defaults ⊕ user overrides (user wins).
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from fnd.config import app_data_dir


@dataclass(slots=True, frozen=True)
class Action:
    """One TUI action — keymap and help-overlay row."""

    id: str  # snake_case name; matches FNDApp.action_<id>
    description: str
    default_key: str | None = None  # None = no default key (palette-only)
    command: str | None = None  # default = id; override for nicer names
    footer_label: str | None = None  # short label for the auto Footer
    show_in_footer: bool = True  # hide noisy actions from footer hints
    # Contexts where this action is relevant for the focus-aware footer.
    # ``()`` (the default) means "always show". Otherwise, only show when
    # the app's focus context matches one of these values. Recognised
    # contexts: ``"query"`` (query input focused), ``"results"`` (results
    # tree focused), ``"preview"`` (preview pane focused).
    contexts: tuple[str, ...] = ()

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
        footer_label="Search",
    ),
    Action(
        id="toggle_focus",
        description="Cycle focus between query bar, results, and preview.",
        default_key="tab",
        footer_label="Pane",
    ),
    Action(
        id="tree_smart_collapse",
        description="Collapse the focused node, or — when the cursor is on "
        "a leaf or an already-collapsed branch — collapse the parent and "
        "move up to it. Lazygit-style 'back out' gesture.",
        default_key="left",
        command="collapse",
        footer_label="Collapse",
        contexts=("results", "collections"),
        show_in_footer=False,
    ),
    Action(
        id="tree_smart_expand",
        description="Expand the focused branch; if already expanded, move "
        "the cursor onto its first child. Right-arrow companion to the "
        "smart-collapse action.",
        default_key="right",
        command="expand",
        footer_label="Expand",
        contexts=("results", "collections"),
        show_in_footer=False,
    ),
    Action(
        id="open_at_locator",
        description="Open the focused result in Skim at its page (PDFs) "
        "or the default app for other formats.",
        default_key="o",
        command="open",
        footer_label="Open",
        contexts=("results", "preview"),
    ),
    Action(
        id="open_default_app",
        description="Open the focused file in the default app (no page jump).",
        default_key="O",
        command="open-default",
        footer_label="Default",
        contexts=("results", "preview"),
        show_in_footer=False,  # noisy alongside Open; reachable via palette
    ),
    Action(
        id="peek_focused",
        description="Quick Look the focused file (no deep-link).",
        default_key="space",
        command="peek",
        footer_label="Peek",
        contexts=("results", "preview"),
    ),
    Action(
        id="show_help",
        description="Toggle help overlay.",
        default_key="question_mark",
        command="help",
        footer_label="Help",
    ),
    Action(
        id="open_command_palette",
        description="Open the command palette to run any action by name.",
        default_key="colon",
        command="palette",
        footer_label="Cmd",
        show_in_footer=False,  # discoverable via help (?)
    ),
    Action(
        id="focus_results_pane",
        description="Focus the results tree.",
        default_key="r",
        command="results",
        footer_label="Results",
        show_in_footer=False,
    ),
    Action(
        id="focus_preview_pane",
        description="Focus the preview pane.",
        default_key="p",
        command="preview",
        footer_label="Preview",
        show_in_footer=False,
    ),
    Action(
        id="focus_filters_panel",
        description="Focus the filters panel.",
        default_key="f",
        command="filters",
        footer_label="Filters",
        show_in_footer=False,
    ),
    Action(
        id="focus_collections_panel",
        description="Focus the collections panel.",
        default_key="c",
        command="collections",
        footer_label="Collections",
        show_in_footer=False,
    ),
    Action(
        id="toggle_highlights",
        description="Toggle search-term highlights in the preview pane "
        "(useful for distraction-free reading without re-running the search).",
        default_key="h",
        command="highlights",
        footer_label="Highlights",
        show_in_footer=False,
    ),
    Action(
        id="toggle_fuzzy",
        description="Toggle auto-fuzzy matching. Persists to config; "
        "per-term ~N still works when auto-fuzzy is off.",
        default_key="ctrl+t",
        command="fuzzy",
        footer_label="Fuzzy",
        show_in_footer=False,
    ),
    Action(
        id="open_collections_form",
        description="Open the Collections form (add / edit / delete collections).",
        default_key=None,
        command="collections-form",
        footer_label="Manage",
        show_in_footer=False,  # advanced; reachable via `:` palette only
    ),
    Action(
        id="open_multi_input",
        description="Open the :multi DSL panel (intent / lex / phrase / syn lines).",
        command="multi",
        footer_label="Multi",
        show_in_footer=False,  # palette-only power-user surface
    ),
    Action(
        id="show_explain_overlay",
        description=("Show JSON trace for the latest search (regime, sub-queries, RRF math)."),
        command="explain",
        footer_label="Explain",
        show_in_footer=False,  # palette-only debugging surface
    ),
    Action(
        id="quit",
        description="Quit fnd.",
        default_key="q",
        footer_label="Quit",
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
                f"[normal][{key!r}] = {aid!r}: unknown action; see `fnd tui` then `:help`"
            )
    return warnings


def resolve_command(name: str) -> Action | None:
    """Map a command-palette name (or action id) to an :class:`Action`."""
    by_id = _registry_index()
    if name in by_id:
        return by_id[name]
    by_cmd = _command_index()
    return by_cmd.get(name)
