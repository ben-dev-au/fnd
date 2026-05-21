"""Unified Settings & Commands menu — data model.

The menu is a tree of :class:`MenuItem` records, one row per item the user
sees in the Settings screen. ``kind`` decides what ``Enter`` does:

  ``header``    Non-selectable group separator row. Two visual levels:
                ``header_level=1`` (top-level, drawn with rule lines) and
                ``header_level=2`` (sub-group, bold-only).
  ``submenu``   Push a new :class:`SettingsScreen` with this item's children.
  ``action``    Run ``FNDApp.action_<action_id>()`` (REGISTRY action) and
                close the settings stack.
  ``scalar``    Open the bottom edit-bar for a single config field. The
                value is written through :func:`fnd.config.write_setting`
                and validated by Pydantic before being committed to disk.
  ``toggle``    Flip a boolean inline. Menu stays open so the user sees the
                new value.
  ``picker``    Push a picker sub-screen with a list of choices.
  ``external``  Run a custom callable that takes the app — used to push
                custom screens (per-source form, delete confirm, rename).

The root menu is one flat scrollable list grouped with ``KIND_HEADER``
rows. The per-collection / per-source drill paths are separate sub-screens
that share the same chrome.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fnd.tui.app import FNDApp


# ── Kinds ────────────────────────────────────────────────────────────

KIND_HEADER = "header"
KIND_SUBMENU = "submenu"
KIND_ACTION = "action"
KIND_SCALAR = "scalar"
KIND_TOGGLE = "toggle"
KIND_PICKER = "picker"
KIND_EXTERNAL = "external"
KIND_DISPLAY = "display"  # read-only: dim label + bright value, no Enter affordance

# Section ids the `?` shortcut pushes directly as a sub-screen.
SECTION_KEYBINDINGS = "keybindings"
SECTION_PREFERENCES = "preferences"
SECTION_COLLECTIONS = "collections"
SECTION_INDEXING = "indexing"


# ── Models ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ChoiceOption:
    """One option in a picker sub-screen."""

    value: Any
    label: str
    description: str = ""


@dataclass(frozen=True)
class MenuItem:
    """One row in the Settings menu.

    Most fields are kind-specific. Unused fields default to empty.
    """

    id: str
    label: str
    description: str = ""
    kind: str = KIND_SUBMENU

    # HEADER: 1 = top-level group, 2 = sub-group.
    header_level: int = 0

    # Key column (Keys & Actions rows). When set, renderer shows it left of
    # the label in a $text-muted 8-char column.
    key: str = ""

    # SUBMENU
    children: tuple[MenuItem, ...] = ()
    provider: Callable[[FNDApp], tuple[MenuItem, ...]] | None = None

    # ACTION
    action_id: str = ""
    # Verb shown inside the trailing `[ ... ]` button affordance. Default
    # "Run". Set to "Delete…" for a destructive confirm, "Open" for a
    # picker open, etc. The `…` suffix is included literally when the
    # action shows a confirm.
    action_label: str = "Run"

    # SCALAR
    setting_path: str = ""
    hint: str = ""
    coerce: Callable[[str], Any] | None = None
    value_getter: Callable[[FNDApp], str] | None = None

    # TOGGLE
    toggle_getter: Callable[[FNDApp], bool] | None = None
    toggle_setter: Callable[[FNDApp, bool], None] | None = None

    # PICKER
    multi: bool = False
    choices_provider: Callable[[FNDApp], list[ChoiceOption]] | None = None
    picker_getter: Callable[[FNDApp], Any] | None = None
    picker_setter: Callable[[FNDApp, Any], None] | None = None

    # EXTERNAL
    external: Callable[[FNDApp], None] | None = None
    # When True, the row launches an OS-level app ($EDITOR, Finder, etc.)
    # rather than pushing an internal Settings screen. Render leading
    # `↗` glyph; trailing slot carries the path (not a drill arrow).
    external_app: bool = False

    # Metadata used by the cross-tree search view.
    keywords: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_header(self) -> bool:
        return self.kind == KIND_HEADER

    @property
    def is_selectable(self) -> bool:
        return self.kind != KIND_HEADER

    def trailing_value(self, app: FNDApp) -> str:
        """Right-aligned trailing column. Setting kinds carry the live
        value; drill rows obey ``drill_summary_mode`` from config."""
        try:
            cfg = getattr(app, "_config", None)
            if cfg is None:
                from fnd.config import load as _load_cfg

                try:
                    cfg = _load_cfg()
                except Exception:
                    cfg = None
            mode: str = (
                cfg.defaults.drill_summary_mode
                if cfg and hasattr(cfg.defaults, "drill_summary_mode")
                else "always_show"
            )
            # Drill rows (KIND_EXTERNAL with a value_getter) obey the mode.
            if self.kind == KIND_EXTERNAL and self.value_getter is not None:
                if mode == "always_ellipsis":
                    return "…"
                if mode == "smart":
                    v = self.value_getter(app)
                    return v if v else "…"
                return self.value_getter(app)
            # Non-drill rows: setting values / toggle states always shown.
            if self.value_getter is not None:
                return self.value_getter(app)
            if self.kind == KIND_TOGGLE and self.toggle_getter is not None:
                return "On" if self.toggle_getter(app) else "Off"
            if self.kind == KIND_PICKER and self.picker_getter is not None:
                v = self.picker_getter(app)
                if isinstance(v, list):
                    return f"{len(v)} selected" if v else "(none)"
                return str(v) if v not in (None, "") else "(unset)"
        except Exception:
            return ""
        return ""

    def resolve_children(self, app: FNDApp) -> tuple[MenuItem, ...]:
        if self.provider is not None:
            try:
                return tuple(self.provider(app))
            except Exception:
                return ()
        return self.children


# ── Header helpers ───────────────────────────────────────────────────


def header(
    label: str,
    *,
    level: int = 1,
    anchor_id: str = "",
    hint: bool = False,
) -> MenuItem:
    """Build a non-selectable header row.

    ``hint=True`` stamps the header (and every body row until the next
    header) with a ``-hint-section`` CSS class via the SettingsList's
    rendering loop — used by the Keybindings cheat sheet to highlight
    the section relevant to the screen that called ``?``.
    """
    keywords: tuple[str, ...] = ("_hint_section_",) if hint else ()
    return MenuItem(
        id=f"header.{anchor_id or label.lower().replace(' ', '_')}",
        label=label,
        kind=KIND_HEADER,
        header_level=level,
        keywords=keywords,
    )


# ── Tree walking ────────────────────────────────────────────────────


def walk_leaves(
    item: MenuItem,
    app: FNDApp,
    breadcrumb: tuple[str, ...] = (),
) -> Iterator[tuple[tuple[str, ...], MenuItem]]:
    """Yield ``(breadcrumb, leaf)`` pairs for every selectable item in
    the tree. Headers are skipped — they're visual structure, not
    discoverable content.
    """
    path = (*breadcrumb, item.label)
    if item.kind == KIND_SUBMENU:
        for child in item.resolve_children(app):
            yield from walk_leaves(child, app, path)
        return
    if item.is_header:
        return
    yield path, item


# ── Keys & Actions content ──────────────────────────────────────────
#
# Each row is a (key, action_id_or_empty, description) tuple. Activating
# the row dispatches the action if action_id is set; built-in widget
# keys (tree navigation, scroll) are listed for discoverability with an
# empty action_id — Enter on those is a no-op (the user invokes them
# directly when the relevant pane has focus).


# Pretty-print a Textual ``default_key`` (e.g. "slash", "ctrl+f") into
# a glyph users recognise on the keyboard.
_KEY_PRETTY: dict[str, str] = {
    "slash": "/",
    "question_mark": "?",
    "colon": ":",
    "space": "Space",
    "tab": "Tab",
    "left": "←",
    "right": "→",
    "up": "↑",
    "down": "↓",
    "enter": "Enter",
    "escape": "Esc",
    "page_up": "PgUp",
    "page_down": "PgDn",
    "home": "Home",
    "end": "End",
    "shift+tab": "Shift+Tab",
    "shift+enter": "Shift+Enter",
}


def _pretty_key(key: str) -> str:
    if key in _KEY_PRETTY:
        return _KEY_PRETTY[key]
    # ctrl+x → Ctrl+X; lone letters stay literal.
    parts = [
        p.capitalize() if p in {"ctrl", "shift", "alt", "cmd"} else p.upper()
        for p in key.split("+")
    ]
    return "+".join(parts) if len(parts) > 1 else key


# Sections that wrap bindings NOT in the Action registry — widget-level
# bindings from SettingsList, SourceFormScreen, OpenWithScreen,
# AccessibilityPermissionScreen, etc. Listed here so the Keybindings
# screen surfaces them too.
#
# Tuple shape: (key, label, action_id, description). Label is the short
# title shown in the row list; description is the longer explanation
# surfaced in the DetailStrip when the row is focused. action_id is
# left blank for widget-level bindings (they don't map to a global
# Action).
_KEYS_SETTINGS: tuple[tuple[str, str, str, str], ...] = (
    (
        "↑ / ↓ / j / k",
        "Move cursor",
        "",
        "Step one row at a time. ↑/↓ are standard; j/k are vi-style aliases. Auto-scrolls into view when the cursor leaves the viewport.",
    ),
    (
        "Enter",
        "Activate",
        "",
        "Open the focused row — picker rows show their chooser, scalar rows open the inline edit bar, drill-in rows push a sub-screen.",
    ),
    (
        "→",
        "Drill in",
        "",
        "Same as Enter for drill-in rows. Convenient when you're already on the arrow cluster.",
    ),
    (
        "←",
        "Back one level",
        "",
        "Pop the current settings screen. Esc does the same; ← only fires when the cursor is on the row list (not the search input).",
    ),
    (
        "/",
        "Filter rows",
        "",
        "Filter rows across the current screen by label, key column, and keywords. Descriptions are excluded — they're advisory, not searchable.",
    ),
    (
        "1-9",
        "Jump by index",
        "",
        "Number keys jump the cursor straight to the nth visible row in the current section.",
    ),
    (
        "Shift+Enter",
        "Reveal in Finder",
        "",
        "On file-pointing rows (config.toml, keybindings.toml, source paths) opens Finder with that file selected. No-op on other rows.",
    ),
    (
        "Esc",
        "Clear search / back",
        "",
        "If the filter is active, clears it first. Press again to pop the current screen.",
    ),
)

_KEYS_SOURCE_FORM: tuple[tuple[str, str, str, str], ...] = (
    (
        "Tab / Shift+Tab",
        "Cycle fields",
        "",
        "Move forward (Tab) or backward (Shift+Tab) through the form fields and the frontmatter sample at the bottom.",
    ),
    (
        "Enter",
        "Edit field",
        "",
        "Edit, pick, or toggle the focused field. Scalar fields open the inline edit bar; multi-select fields push a picker.",
    ),
    (
        "Ctrl+S",
        "Save & close",
        "",
        "Persist this source to config.toml. Triggers an async reindex if the source set or includes/excludes changed.",
    ),
    (
        "Ctrl+D",
        "Delete source",
        "",
        "Only available when editing an existing source. Pushes a confirmation modal; on confirm, removes the entry from config and reindexes.",
    ),
    (
        "Esc / ←",
        "Cancel",
        "",
        "Discard unsaved changes and pop back to the Sources screen.",
    ),
)

_KEYS_OPEN_WITH: tuple[tuple[str, str, str, str], ...] = (
    (
        "↑ / ↓ / j / k",
        "Move between apps",
        "",
        "Step between the apps eligible for this hit's file type. The cursor parks on the resolved default (★) by default.",
    ),
    (
        "Enter",
        "Open with default",
        "",
        "Fire the highlighted (★) app — the one the resolver would have used for `o`. ★ shows which app fnd thinks is best for this hit.",
    ),
    (
        "a-z",
        "Letter shortcut",
        "",
        "Press the bold letter on any row to fire that app directly without arrow-key navigation.",
    ),
    (
        "Esc / q",
        "Cancel",
        "",
        "Dismiss the picker without opening anything.",
    ),
)

_KEYS_AX_MODAL: tuple[tuple[str, str, str, str], ...] = (
    (
        "o",
        "Open System Settings",
        "",
        "Jumps to System Settings → Privacy & Security → Accessibility so you can grant fnd permission for the Preview AppleScript page-jump.",
    ),
    (
        "r",
        "Retry",
        "",
        "Re-check AX permission without restarting fnd. Use after granting permission to clear the cached 'not trusted' state.",
    ),
    (
        "Esc / q",
        "Dismiss",
        "",
        "Close the modal. AX permission isn't required to use fnd — Preview will just open at page 1 without it.",
    ),
)


# Mapping from an Action's primary context (first entry of
# ``contexts``) to the section header it lands under in the
# Keybindings screen. ``""`` (no contexts) → Global.
_CONTEXT_TO_SECTION: dict[str, str] = {
    "": "Global",
    "results": "Results pane",
    "preview": "Preview pane",
    "filters": "Filters panel",
    "collections": "Collections panel",
    "query": "Query input",
}


_ID_SAFE_RE = re.compile(r"[^a-z0-9]+")


def _slug(*parts: str) -> str:
    """Join ``parts`` into a lowercased, ascii-safe slug for MenuItem
    ids. Keybinding labels like 'Cancel' repeat across sections, and
    keys like '↑ / ↓ / j / k' or 'Esc / ←' contain spaces and arrows
    that break id syntax; this collapses both into one safe form."""
    return _ID_SAFE_RE.sub("_", "_".join(p.lower() for p in parts if p)).strip("_")


def _key_row(
    key: str,
    label: str,
    action_id: str,
    description: str,
    *,
    section: str = "",
) -> MenuItem:
    """Build a Keybindings cheat-sheet row. ``label`` is the short title
    shown in the row list; ``description`` is the long-form explanation
    surfaced in the DetailStrip when the row is focused — DON'T pass
    the same string for both, or the DetailStrip just echoes the row.

    ``section`` disambiguates widget-only rows (action_id="") that
    share a label across sections (Source form's "Cancel" vs Open
    with's "Cancel"); without it, both would collapse to ``key.cancel``
    and the second mount would shadow the first.
    """
    item_id = f"key.{action_id}" if action_id else "key." + _slug(section, key, label)
    return MenuItem(
        id=item_id,
        label=label,
        description=description,
        kind=KIND_ACTION,
        action_id=action_id,
        key=key,
        keywords=(action_id, key) if action_id else (key,),
    )


def _action_label(action: Any) -> str:
    """Pick the short label for an Action's keybindings row. Preference
    order: ``footer_label`` (already crafted for the auto-footer) →
    ``command`` (the palette name) → titlecased ``id``."""
    if action.footer_label:
        return str(action.footer_label)
    if action.command:
        return str(action.command).replace("_", " ").title()
    return str(action.id).replace("_", " ").title()


# ── Providers ───────────────────────────────────────────────────────


def _provider_keybindings(_app: FNDApp, *, context_hint: str | None = None) -> tuple[MenuItem, ...]:
    """Build the Keybindings list from the live ``Action`` registry plus
    static widget-binding tables.

    Single source of truth = ``fnd.tui.actions.REGISTRY``. Sub-sections:

    * Global — actions with no ``contexts`` constraint.
    * Per-pane sections (Results / Preview / Query / Filters / Collections)
      — actions whose primary (first) context matches that pane.
    * Static sections — Settings menu, Source form, Open-with modal,
      Accessibility prompt. These live in widget BINDINGS, not the
      registry, so they're hand-curated; they're at least short.

    ``context_hint`` is the section label that should be moved to the
    top (right after Global) — e.g. "Source form" when the user pressed
    ``?`` from inside the source-edit form. Empty sections are dropped.
    """
    from fnd.tui.actions import REGISTRY

    sections: dict[str, list[MenuItem]] = {
        "Global": [],
        "Results pane": [],
        "Preview pane": [],
        "Query input": [],
        "Filters panel": [],
        "Collections panel": [],
    }
    for action in REGISTRY:
        if action.default_key is None:
            continue  # palette-only — no key to show
        primary_ctx = action.contexts[0] if action.contexts else ""
        section = _CONTEXT_TO_SECTION.get(primary_ctx, "Global")
        sections[section].append(
            _key_row(
                _pretty_key(action.default_key),
                _action_label(action),
                action.id,
                action.description,
            )
        )

    # Static widget bindings — append AFTER the registry-derived
    # sections in declaration order; reordering happens below.
    # Pass section name so widget-only rows that share labels across
    # sections (multiple "Cancel" rows) get distinct MenuItem ids.
    sections["Settings menu"] = [_key_row(*row, section="settings") for row in _KEYS_SETTINGS]
    sections["Source form"] = [_key_row(*row, section="source_form") for row in _KEYS_SOURCE_FORM]
    sections["Open with… modal"] = [_key_row(*row, section="open_with") for row in _KEYS_OPEN_WITH]
    sections["Accessibility prompt"] = [
        _key_row(*row, section="ax_modal") for row in _KEYS_AX_MODAL
    ]

    # Display order: Global first; then hint section (if set and not
    # Global); then everything else in declaration order; empty
    # sections dropped.
    order = list(sections.keys())
    if context_hint and context_hint in sections and context_hint != "Global":
        order.remove(context_hint)
        order.insert(1, context_hint)

    out: list[MenuItem] = []
    for section in order:
        rows = sections[section]
        if not rows:
            continue
        is_hint = bool(context_hint) and section == context_hint
        out.append(header(section, level=2, hint=is_hint))
        out.extend(rows)
    return tuple(out)


def _setting_writer(path: str) -> Callable[[FNDApp, Any], None]:
    """Setter that writes one config field and reloads the app's cached
    Config / ranking profile."""

    def _set(app: FNDApp, value: Any) -> None:
        from fnd.config import default_config_path, load, write_setting

        write_setting(
            config_path=default_config_path(),
            dotted_path=path,
            value=value,
        )
        app._config = load()  # type: ignore[attr-defined]
        app._ranking_profile = app._resolve_profile()  # type: ignore[attr-defined]
        app._refresh_status()  # type: ignore[attr-defined]

    return _set


def _get_int_default(field_name: str, fallback: int) -> Callable[[FNDApp], str]:
    def _g(app: FNDApp) -> str:
        cfg = app._config  # type: ignore[attr-defined]
        return str(getattr(cfg.defaults, field_name)) if cfg else str(fallback)

    return _g


def _get_float_default(field_name: str, fallback: float) -> Callable[[FNDApp], str]:
    def _g(app: FNDApp) -> str:
        cfg = app._config  # type: ignore[attr-defined]
        return str(getattr(cfg.defaults, field_name)) if cfg else str(fallback)

    return _g


def _get_default_collection(app: FNDApp) -> Any:
    cfg = app._config  # type: ignore[attr-defined]
    return cfg.defaults.collection if cfg else ""


def _choices_collections(app: FNDApp) -> list[ChoiceOption]:
    cfg = app._config  # type: ignore[attr-defined]
    if cfg is None:
        return []
    return [ChoiceOption(value=n, label=n) for n in sorted(cfg.collections)]


def _choices_ranking(app: FNDApp) -> list[ChoiceOption]:
    cfg = app._config  # type: ignore[attr-defined]
    if cfg is None:
        return []
    return [ChoiceOption(value=n, label=n) for n in sorted(cfg.ranking)]


def _set_highlights(app: FNDApp, value: bool) -> None:
    if app._highlights_enabled != value:  # type: ignore[attr-defined]
        app.action_toggle_highlights()


def _provider_preferences(_app: FNDApp) -> tuple[MenuItem, ...]:
    """Content of the Preferences sub-screen — scalars / toggles / pickers
    grouped by area."""
    return (
        header("Search behaviour", level=2),
        MenuItem(
            id="pref.result_limit",
            label="Result limit",
            description="Max results returned per query (1-1000).",
            kind=KIND_SCALAR,
            setting_path="defaults.result_limit",
            hint="1-1000",
            coerce=int,
            value_getter=_get_int_default("result_limit", 200),
            keywords=("result", "limit"),
        ),
        MenuItem(
            id="pref.debounce_ms",
            label="Debounce (ms)",
            description="Wait this many ms after the last keystroke before re-running.",
            kind=KIND_SCALAR,
            setting_path="defaults.debounce_ms",
            hint="0-2000",
            coerce=int,
            value_getter=_get_int_default("debounce_ms", 200),
            keywords=("debounce", "delay"),
        ),
        MenuItem(
            id="pref.preview_load_debounce_ms",
            label="Preview load debounce (ms)",
            description=(
                "Idle delay before a results-tree cursor move triggers a preview "
                "load. Lets you sweep down the list without freezing at each row."
            ),
            kind=KIND_SCALAR,
            setting_path="defaults.preview_load_debounce_ms",
            hint="0-1000",
            coerce=int,
            value_getter=_get_int_default("preview_load_debounce_ms", 150),
            keywords=("preview", "debounce", "delay", "load"),
        ),
        MenuItem(
            id="pref.preview_chunks",
            label="Preview chunks",
            description="How many chunks to render in the preview pane (1-50).",
            kind=KIND_SCALAR,
            setting_path="defaults.preview_chunks",
            hint="1-50",
            coerce=int,
            value_getter=_get_int_default("preview_chunks", 5),
            keywords=("preview", "chunks"),
        ),
        MenuItem(
            id="pref.sections_score_threshold",
            label="Section score threshold",
            description=(
                "Per-file: keep sections whose score is at least "
                "(threshold × top section's score). 0.0 = show every "
                "match (subject to cap); 1.0 = only the top section."
            ),
            kind=KIND_SCALAR,
            setting_path="defaults.sections_score_threshold",
            hint="0.0-1.0",
            coerce=float,
            value_getter=_get_float_default("sections_score_threshold", 0.5),
            keywords=("section", "threshold", "score", "filter"),
        ),
        MenuItem(
            id="pref.sections_per_file_max",
            label="Section cap per file",
            description=(
                "Hard cap on sections surfaced for one file, applied "
                "after the score threshold. Safety net for files with "
                "thousands of matches."
            ),
            kind=KIND_SCALAR,
            setting_path="defaults.sections_per_file_max",
            hint="1-2000",
            coerce=int,
            value_getter=_get_int_default("sections_per_file_max", 200),
            keywords=("section", "cap", "limit"),
        ),
        MenuItem(
            id="pref.preview_decode_workers",
            label="Preview decode workers",
            description=(
                "Thread count for parallel chunk decode during preview "
                "load. tantivy releases the GIL for doc reads, so "
                "threads help on huge PDFs. 1 = serial; bump (4-8) for "
                "big files."
            ),
            kind=KIND_SCALAR,
            setting_path="defaults.preview_decode_workers",
            hint="1-16",
            coerce=int,
            value_getter=_get_int_default("preview_decode_workers", 4),
            keywords=("preview", "decode", "workers", "threads", "parallel"),
        ),
        MenuItem(
            id="pref.fuzzy_enabled",
            label="Auto-fuzzy matching",
            description=(
                "Widen the cascade fallback to match typo'd query terms. "
                "Per-term ``~N`` in the query still works when this is off."
            ),
            kind=KIND_TOGGLE,
            toggle_getter=lambda app: (  # type: ignore[arg-type]
                app._config.defaults.fuzzy_enabled  # type: ignore[attr-defined]
                if app._config  # type: ignore[attr-defined]
                else True
            ),
            toggle_setter=lambda app, v: _setting_writer("defaults.fuzzy_enabled")(app, v),
            keywords=("fuzzy", "typo", "search", "match"),
        ),
        MenuItem(
            id="pref.fuzzy_min_term_chars",
            label="Auto-fuzzy minimum term length",
            description=(
                "Minimum post-stem length for auto-fuzzy. Stems shorter "
                "than this are exact-only. Raise to 4/5 to suppress "
                "fuzzy on common short words."
            ),
            kind=KIND_SCALAR,
            setting_path="defaults.fuzzy_min_term_chars",
            hint="0-10",
            coerce=int,
            value_getter=_get_int_default("fuzzy_min_term_chars", 3),
            keywords=("fuzzy", "min", "length", "chars", "floor"),
        ),
        header("Display", level=2),
        MenuItem(
            id="pref.highlights",
            label="Highlights",
            description="Search-term highlights in the preview pane.",
            kind=KIND_TOGGLE,
            toggle_getter=lambda app: app._highlights_enabled,  # type: ignore[attr-defined]
            toggle_setter=_set_highlights,
            keywords=("highlight",),
        ),
        MenuItem(
            id="pref.drill_summary_mode",
            label="Drill row summaries",
            description="How drill-in rows render their trailing column.",
            kind=KIND_PICKER,
            choices_provider=lambda _app: [
                ChoiceOption(value="always_show", label="Always show summary"),
                ChoiceOption(value="smart", label="Smart (only when informative)"),
                ChoiceOption(value="always_ellipsis", label="Always show … only"),
            ],
            picker_getter=lambda app: (  # type: ignore[arg-type]
                app._config.defaults.drill_summary_mode  # type: ignore[attr-defined]
                if app._config  # type: ignore[attr-defined]
                else "always_show"
            ),
            picker_setter=_setting_writer("defaults.drill_summary_mode"),
            keywords=("drill", "summary", "trailing"),
        ),
        header("Defaults", level=2),
        MenuItem(
            id="pref.default_collection",
            label="Default collection",
            description="Active collection when --collection is omitted.",
            kind=KIND_PICKER,
            choices_provider=_choices_collections,
            picker_getter=_get_default_collection,
            picker_setter=_setting_writer("defaults.collection"),
            keywords=("default", "collection"),
        ),
        header("Default app per filetype", level=2),
        *_filetype_default_app_items(),
    )


# ── Default-app pickers per filetype ────────────────────────────────


_FILETYPE_LABELS: dict[str, str] = {
    "pdf": "PDF",
    "md": "Markdown",
    "txt": "Plain text",
    "docx": "Word",
    "pptx": "PowerPoint",
}


def _filetype_default_app_items() -> tuple[MenuItem, ...]:
    """One picker per indexer-supported filetype. Picker lists every
    registered app whose ``handles`` covers that kind (built-ins +
    user-defined), plus a "(auto-resolve)" sentinel that clears the
    explicit default and lets the resolver walk its own ladder
    (per-source → auto-promote → system)."""
    rows: list[MenuItem] = []
    for kind, label in _FILETYPE_LABELS.items():
        rows.append(
            MenuItem(
                id=f"pref.app_defaults.{kind}",
                label=f"Default {label} app",
                description=(
                    f"App that opens {label} files when no per-source "
                    "override is set. '(auto-resolve)' lets the resolver "
                    "auto-pick (eg. Skim if installed → Preview-if-AX → system)."
                ),
                kind=KIND_PICKER,
                choices_provider=lambda app, k=kind: _choices_apps_for_kind(app, k),
                picker_getter=lambda app, k=kind: _get_app_default_for_kind(app, k),
                picker_setter=lambda app, value, k=kind: _set_app_default_for_kind(app, k, value),
                keywords=("app", "default", kind, label.lower(), "filetype"),
            )
        )
    return tuple(rows)


def _choices_apps_for_kind(app: FNDApp, kind: str) -> list[ChoiceOption]:
    """Apps registered for ``kind`` (or wildcard) + an auto-resolve
    sentinel. Available-only so the picker doesn't offer apps that
    aren't installed."""
    from fnd.apps import build_registry

    cfg = app._config  # type: ignore[attr-defined]
    registry = build_registry(cfg)
    out: list[ChoiceOption] = [
        ChoiceOption(
            value="",
            label="(auto-resolve)",
            description="Let the resolver pick — skim → preview-if-AX → system for PDFs; system for others.",
        )
    ]
    for app_id, app_def in registry.items():
        if kind not in app_def.handles and "*" not in app_def.handles:
            continue
        if not app_def.available():
            continue
        out.append(
            ChoiceOption(
                value=app_id,
                label=app_def.display_name,
                description=app_def.notes or "",
            )
        )
    return out


def _get_app_default_for_kind(app: FNDApp, kind: str) -> str:
    cfg = app._config  # type: ignore[attr-defined]
    if cfg is None:
        return ""
    return cfg.app_defaults.get(kind, "")


def _set_app_default_for_kind(app: FNDApp, kind: str, value: Any) -> None:
    """Persist the picker choice. Empty value (auto-resolve sentinel)
    clears the explicit default — done by reloading config, removing
    the key, and writing the whole [app_defaults] back via tomlkit."""
    from fnd.config import default_config_path, load, write_setting

    cfg_path = default_config_path()
    if value:
        write_setting(config_path=cfg_path, dotted_path=f"app_defaults.{kind}", value=value)
    else:
        # No write_unset helper — round-trip via tomlkit to drop the key.
        import tomlkit

        if cfg_path.exists():
            doc = tomlkit.parse(cfg_path.read_text(encoding="utf-8"))
            defaults = doc.get("app_defaults")
            if defaults is not None and kind in defaults:
                del defaults[kind]
                from fnd._perms import secure_write_text

                secure_write_text(cfg_path, tomlkit.dumps(doc))
    app._config = load()  # type: ignore[attr-defined]
    app._refresh_status()  # type: ignore[attr-defined]


# ── Collections drill chain (per-collection / per-source) ───────────


def _collection_summary(app: FNDApp, name: str) -> str:
    """Trailing slot for a collection row in the Collections sub-screen —
    shows scope dot, source count, and ranking profile."""
    cfg = app._config  # type: ignore[attr-defined]
    if cfg is None or name not in cfg.collections:
        return ""
    coll = cfg.collections[name]
    n = len(coll.sources)
    active = "●" if name in (app._collections or []) else "○"  # type: ignore[attr-defined]
    profile = getattr(coll, "ranking_profile", None) or "default"
    return f"{active} {n} source{'s' if n != 1 else ''} · ranking:{profile}"


def _make_open_collection_screen(name: str) -> Callable[[FNDApp], None]:
    """Push a sub-SettingsScreen for managing ``name``."""

    def _open(app: FNDApp) -> None:
        from fnd.tui.settings_screen import SettingsScreen

        items = _provider_collection(app, name)
        app.push_screen(
            SettingsScreen(
                breadcrumb=("Collections", name),
                items=items,
                provider=lambda a, _n=name: tuple(_provider_collection(a, _n)),
            )
        )

    return _open


def _make_open_sources_screen(name: str) -> Callable[[FNDApp], None]:
    def _open(app: FNDApp) -> None:
        from fnd.tui.settings_screen import SettingsScreen

        items = _provider_sources(app, name)
        app.push_screen(
            SettingsScreen(
                breadcrumb=("Collections", name, "Sources"),
                items=items,
                provider=lambda a, _n=name: tuple(_provider_sources(a, _n)),
            )
        )

    return _open


def _make_open_source_form(name: str, index: int | None) -> Callable[[FNDApp], None]:
    """index=None means 'add new source'."""

    def _open(app: FNDApp) -> None:
        from fnd.tui.settings_screen import SourceFormScreen

        app.push_screen(SourceFormScreen(collection_name=name, source_index=index))

    return _open


def _make_open_rename(name: str) -> Callable[[FNDApp], None]:
    def _open(app: FNDApp) -> None:
        from fnd.tui.settings_screen import RenameCollectionScreen

        app.push_screen(RenameCollectionScreen(collection_name=name))

    return _open


def _make_open_delete_confirm(name: str) -> Callable[[FNDApp], None]:
    def _open(app: FNDApp) -> None:
        from fnd.tui.settings_screen import DeleteCollectionScreen

        app.push_screen(DeleteCollectionScreen(collection_name=name))

    return _open


def _make_reindex(name: str) -> Callable[[FNDApp], None]:
    def _run(app: FNDApp) -> None:
        # Route through the warning + IndexerScreen modal so the user
        # sees progress instead of a silent background task.
        app._reindex_with_warning_if_needed(name)  # type: ignore[attr-defined]

    return _run


def _make_add_collection() -> Callable[[FNDApp], None]:
    def _open(app: FNDApp) -> None:
        from fnd.tui.settings_screen import AddCollectionWizard

        app.push_screen(AddCollectionWizard())

    return _open


def _open_config_file_action(app: FNDApp) -> None:
    app.action_open_config_file()


def _open_keybindings_file_action(app: FNDApp) -> None:
    app.action_open_keybindings_file()  # type: ignore[attr-defined]


def _provider_collections(app: FNDApp) -> tuple[MenuItem, ...]:
    """Content of the Collections sub-screen.

    Two actions at the top — Add collection + Update all collections —
    then one drill-in row per configured collection."""
    cfg = app._config  # type: ignore[attr-defined]
    names = sorted(cfg.collections.keys()) if cfg else []
    items: list[MenuItem] = [
        MenuItem(
            id="collections.add",
            label="Add collection",
            description=(
                "Open the new-collection wizard — pick a name, then add "
                "the first source. The collection becomes available as "
                "`--collection <name>` once at least one source is indexed."
            ),
            kind=KIND_ACTION,
            action_label="Add",
            external=_make_add_collection(),
            keywords=("add", "new", "create"),
        ),
        MenuItem(
            id="collections.update_all",
            label="Update all collections",
            description=(
                "Run Update index for every collection in sequence. "
                "Same per-file rules as the per-collection Update index — "
                "unchanged files are skipped, the PDF structure cache "
                "is consulted, not cleared."
            ),
            kind=KIND_ACTION,
            action_label="Update",
            external=_run_update_all_collections,
            value_getter=_summary_update_all,
            keywords=("update", "all", "index", "reindex", "everything"),
        ),
    ]
    for name in names:
        items.append(
            MenuItem(
                id=f"collection.{name}",
                label=name,
                description=f"Edit sources, ranking profile, or update / delete the {name} collection.",
                kind=KIND_EXTERNAL,
                external=_make_open_collection_screen(name),
                value_getter=(lambda n: lambda app: _collection_summary(app, n))(name),
                keywords=(name,),
            )
        )
    return tuple(items)


def _summary_update_all(app: FNDApp) -> str:
    """Trailing context for the Update all collections action — count
    of collections + rough total file count if known."""
    cfg = app._config  # type: ignore[attr-defined]
    if cfg is None:
        return ""
    n_collections = len(cfg.collections)
    if n_collections == 0:
        return "no collections"
    n_sources = sum(len(c.sources) for c in cfg.collections.values())
    return f"{n_collections} collections · {n_sources} sources"


def _run_update_all_collections(app: FNDApp) -> None:
    """Push a confirm dialog, then iterate every collection through
    the existing IndexerScreen path on Yes."""
    import contextlib

    from fnd.tui.settings_screen import UpdateAllConfirm

    cfg = app._config  # type: ignore[attr-defined]
    if cfg is None or not cfg.collections:
        with contextlib.suppress(Exception):
            app.notify("No collections to update.")
        return
    names = sorted(cfg.collections.keys())
    app.push_screen(UpdateAllConfirm(collection_names=names))


def _provider_collection(app: FNDApp, name: str) -> tuple[MenuItem, ...]:
    """Per-collection sub-menu."""
    return (
        MenuItem(
            id=f"col.{name}.rename",
            label="Rename",
            kind=KIND_EXTERNAL,
            external=_make_open_rename(name),
        ),
        MenuItem(
            id=f"col.{name}.sources",
            label="Sources",
            kind=KIND_EXTERNAL,
            external=_make_open_sources_screen(name),
            value_getter=(
                lambda n: (
                    lambda app: (
                        f"{len(app._config.collections[n].sources)} source(s)"  # type: ignore[attr-defined]
                        if app._config and n in app._config.collections  # type: ignore[attr-defined]
                        else ""
                    )
                )
            )(name),
        ),
        MenuItem(
            id=f"col.{name}.ranking_profile",
            label="Ranking profile",
            kind=KIND_PICKER,
            choices_provider=_choices_ranking,
            picker_getter=(
                lambda n: (
                    lambda app: (
                        app._config.collections[n].ranking_profile  # type: ignore[attr-defined]
                        if app._config and n in app._config.collections  # type: ignore[attr-defined]
                        else ""
                    )
                )
            )(name),
            picker_setter=(
                lambda n: lambda app, value: _set_collection_ranking_profile(app, n, value)
            )(name),
        ),
        MenuItem(
            id=f"col.{name}.reindex",
            label="Update index now",
            description=(
                "Re-scan this collection's sources. New / changed files are added; "
                "deleted files are removed; unchanged files are skipped. Uses the "
                "PDF structure cache to skip extraction when content hasn't changed."
            ),
            kind=KIND_ACTION,
            action_label="Update",
            external=_make_reindex(name),
        ),
        MenuItem(
            id=f"col.{name}.delete",
            label="Delete collection",
            description=(
                "Remove this collection from config and drop its chunks from the "
                "search index. Other collections are unaffected."
            ),
            kind=KIND_ACTION,
            action_label="Delete…",
            external=_make_open_delete_confirm(name),
        ),
    )


def _set_collection_ranking_profile(app: FNDApp, name: str, value: Any) -> None:
    """Picker setter for a per-collection ranking_profile field."""
    from fnd.config import default_config_path, load, write_setting

    write_setting(
        config_path=default_config_path(),
        dotted_path=f"collections.{name}.ranking_profile",
        value=value,
    )
    app._config = load()  # type: ignore[attr-defined]
    app._ranking_profile = app._resolve_profile()  # type: ignore[attr-defined]
    app._refresh_status()  # type: ignore[attr-defined]


def _source_trailing(collection_name: str, idx: int) -> Callable[[FNDApp], str]:
    """Build a value_getter for a per-source row that shows file-types
    and a path-not-found warning when the source directory is missing."""

    def _summary(app: FNDApp) -> str:
        from fnd.config import INDEXER_FILETYPES

        cfg = app._config  # type: ignore[attr-defined]
        if cfg is None or collection_name not in cfg.collections:
            return ""
        sources = cfg.collections[collection_name].sources
        if idx >= len(sources):
            return ""
        src = sources[idx]
        # Derive display extensions from glob patterns in src.includes.
        exts: list[str] = []
        for glob in src.includes:
            for ext in INDEXER_FILETYPES:
                if glob.endswith(f".{ext}"):
                    exts.append(ext)
                    break
        types = ", ".join(exts) if exts else "Custom"
        suffix = ""
        try:
            p = Path(src.path)
            if not p.exists():
                suffix = " · ⚠ path not found"
        except Exception:
            suffix = " · ⚠ path not found"
        return f"{types}{suffix}"

    return _summary


def _make_open_clone_source(name: str) -> Callable[[FNDApp], None]:
    def _open(app: FNDApp) -> None:
        from fnd.tui.settings_screen import CloneSourcePickCollectionScreen

        app.push_screen(CloneSourcePickCollectionScreen(target_collection=name))

    return _open


def _provider_sources(app: FNDApp, name: str) -> tuple[MenuItem, ...]:
    """Per-collection Sources list."""
    cfg = app._config  # type: ignore[attr-defined]
    if cfg is None or name not in cfg.collections:
        return ()
    items: list[MenuItem] = [
        MenuItem(
            id=f"sources.{name}.add",
            label="Add source",
            kind=KIND_EXTERNAL,
            external=_make_open_source_form(name, None),
        ),
        MenuItem(
            id=f"sources.{name}.clone",
            label="Clone from another collection…",
            description=(
                "Deep-copy a source from another collection into this "
                "one. Useful when you keep the same root (eg. an "
                "Obsidian vault) in several collections with different "
                "filters."
            ),
            kind=KIND_EXTERNAL,
            external=_make_open_clone_source(name),
            keywords=("clone", "copy", "another", "collection"),
        ),
    ]
    col = cfg.collections[name]
    for i, src in enumerate(col.sources):
        path_display = str(src.path) if src.path else "(no path)"
        items.append(
            MenuItem(
                id=f"sources.{name}.{i}",
                label=f"{i + 1}. {Path(path_display).name or path_display}",
                description=path_display,
                kind=KIND_EXTERNAL,
                external=_make_open_source_form(name, i),
                value_getter=_source_trailing(name, i),
            )
        )
    return tuple(items)


# ── Configuration ──────────────────────────────────────────────────


# ── Root menu (small list of drill-in categories) ───────────────────


def _open_section(section_id: str) -> Callable[[FNDApp], None]:
    """Build an external callable that pushes the named sub-screen."""

    def _open(app: FNDApp) -> None:
        from fnd.tui.settings_screen import open_settings_section

        open_settings_section(app, section_id)

    return _open


def _summary_preferences(_app: FNDApp) -> str:
    return "Result limit · Debounce · Highlights · Defaults"


def _summary_collections(app: FNDApp) -> str:
    cfg = app._config  # type: ignore[attr-defined]
    if cfg is None:
        from fnd.config import load as _load_config

        try:
            cfg = _load_config()
        except Exception:
            cfg = None
    if cfg is None:
        return "0 collections"
    n_collections = len(cfg.collections)
    n_sources = sum(len(c.sources) for c in cfg.collections.values())
    return (
        f"{n_collections} collection{'s' if n_collections != 1 else ''}"
        f" · {n_sources} source{'s' if n_sources != 1 else ''}"
    )


def _summary_keybindings(app: FNDApp) -> str:
    keymap = app._fnd_keymap  # type: ignore[attr-defined]
    n_keys = len(keymap.bindings)
    return f"{n_keys} keys across 6 contexts"


def _summary_config_path(_app: FNDApp) -> str:
    from fnd.config import default_config_path

    p = str(default_config_path())
    return ("…" + p[-50:]) if len(p) > 50 else p


def _summary_keybindings_path(_app: FNDApp) -> str:
    from fnd.config import default_config_path

    p = str(default_config_path().parent / "keybindings.toml")
    return ("…" + p[-50:]) if len(p) > 50 else p


# ── Indexing section ────────────────────────────────────────────────


def _provider_indexing(_app: FNDApp) -> tuple[MenuItem, ...]:
    """Indexing sub-screen.

    Three groups: structured PDF (status + install/uninstall), reindex
    behaviour (auto-resume toggle), and PDF structure cache (size display
    + maintenance drill)."""
    return (
        header("Structured PDF extraction", level=2),
        MenuItem(
            id="indexing.pdf_status",
            label="Status",
            description=(
                "Whether structured-PDF extraction is active. When installed, "
                "the next Update index will populate the cache for any PDF "
                "not already cached. When not installed, PDFs render as flat text."
            ),
            kind=KIND_DISPLAY,
            value_getter=_summary_pdf_status,
            keywords=("pdf", "structure", "pdf-structure", "status", "extra", "installed"),
        ),
        MenuItem(
            id="indexing.pdf_install",
            label=_pdf_install_label(),
            description=(
                "Open the disclosure + confirm screen. Installs pymupdf4llm[layout] "
                "(Polyform NC, ~200 MB) + docling-slim[standard] (Apache-2.0, ~700 MB). "
                "ML model weights download on first use."
            ),
            kind=KIND_ACTION,
            external=_open_pdf_install_confirm,
            action_label=_pdf_install_verb(),
            keywords=(
                "pdf",
                "structure",
                "install",
                "uninstall",
                "pdf-structure",
                "extra",
                "pymupdf4llm",
                "docling",
            ),
        ),
        header("PDF structure cache", level=2),
        MenuItem(
            id="indexing.cache_size",
            label="Size",
            description=(
                "Per-file structured chunks. Shared across collections — same file "
                "in two collections is extracted once and reused. Pruning or "
                "clearing only affects the next Update index."
            ),
            kind=KIND_DISPLAY,
            value_getter=_summary_cache_size_row,
            keywords=("cache", "size", "entries", "extraction"),
        ),
        MenuItem(
            id="indexing.cache_location",
            label="Location",
            description=(
                "Disk path. Safe to delete from outside fnd; the next Update "
                "index will re-create the directory as needed."
            ),
            kind=KIND_DISPLAY,
            value_getter=_summary_cache_location_row,
            keywords=("cache", "location", "path", "disk"),
        ),
        MenuItem(
            id="indexing.cache_update",
            label="Update cache",
            description=(
                "Populate the PDF structure cache for every PDF in any "
                "collection's sources that doesn't have an entry yet. Doesn't "
                "touch the search index — runs only the structuring pipeline. "
                "Use to pre-warm before a big Update index."
            ),
            kind=KIND_ACTION,
            action_label="Update",
            external=_run_update_cache,
            value_getter=_summary_cache_update,
            keywords=("cache", "update", "populate", "warm", "structure"),
        ),
        MenuItem(
            id="indexing.cache_prune",
            label="Prune stale entries",
            description=(
                "Remove cache entries whose extractor signature doesn't match "
                "the current extractor. Fresh entries stay. Files with pruned "
                "entries get re-extracted on the next Update index."
            ),
            kind=KIND_ACTION,
            action_label="Prune…",
            external=_run_cache_prune,
            value_getter=_summary_stale_entries,
            keywords=("cache", "prune", "stale", "extractor", "signature"),
        ),
        MenuItem(
            id="indexing.cache_clear",
            label="Clear PDF structure cache",
            description=(
                "Wipe the entire cache. PDFs render as flat text until the next "
                "Update index, which will re-extract every PDF — see the cost "
                "estimate before confirming."
            ),
            kind=KIND_ACTION,
            action_label="Clear…",
            external=_run_cache_clear,
            keywords=("cache", "clear", "delete", "wipe", "reset"),
        ),
        header("Behaviour", level=2),
        MenuItem(
            id="indexing.auto_resume",
            label="Auto-resume on launch",
            description=(
                "When On, fnd resumes an interrupted Update index silently "
                "in the background next time you open the app. When Off, "
                "you have to trigger Update index manually after a quit."
            ),
            kind=KIND_TOGGLE,
            toggle_getter=_get_indexer_auto_resume,
            toggle_setter=lambda app, v: _setting_writer("defaults.indexer_auto_resume")(app, v),
            setting_path="defaults.indexer_auto_resume",
            keywords=("auto", "resume", "indexer", "interrupted", "launch", "reindex"),
        ),
        MenuItem(
            id="indexing.cache_at_index_time",
            label="Update cache at index time",
            description=(
                "When On (default), Update index also writes fresh cache entries "
                "for any PDFs not already cached. When Off, Update index uses "
                "cached entries on hit but skips fresh extraction — fast flat-text "
                "refresh, useful on battery."
            ),
            kind=KIND_TOGGLE,
            toggle_getter=_get_cache_at_index_time,
            toggle_setter=lambda app, v: _setting_writer("defaults.cache_at_index_time")(app, v),
            setting_path="defaults.cache_at_index_time",
            keywords=("cache", "index", "time", "extract", "battery", "fast"),
        ),
    )


def _is_pdf_structure_installed() -> bool:
    from fnd.extras import EXTRAS, is_extra_installed

    extra = EXTRAS.get("pdf-structure")
    return extra is not None and is_extra_installed(extra)


def _summary_pdf_status(app: FNDApp) -> str:
    """Trailing for the Status row inside Structured PDF extraction.

    Format: '✓ Installed · ~N MB' or '✗ Not installed · ~N MB to install'.
    The `actual_disk_mb` walk is slow so we route through the lazy
    cache; first paint shows ``…`` then the real value lands."""
    from fnd.tui.lazy_trailing import get_or_schedule

    def _compute() -> str:
        from fnd.extras import EXTRAS, actual_disk_mb

        extra = EXTRAS.get("pdf-structure")
        if extra is None:
            return "(unavailable)"
        if _is_pdf_structure_installed():
            return f"✓ Installed · ~{actual_disk_mb(extra)} MB"
        est = sum(p.disk_mb for p in extra.packages)
        return f"✗ Not installed · ~{est} MB to install"

    return get_or_schedule(app, "indexing.pdf_status", _compute)


def _pdf_install_label() -> str:
    return "Uninstall pdf-structure" if _is_pdf_structure_installed() else "Install pdf-structure"


def _pdf_install_verb() -> str:
    """Trailing-button verb for the pdf-structure install row.

    Mirrors the label's intent so `[ Install ]` / `[ Uninstall ]`
    reads naturally next to the row, not a generic `[ Open ]` that
    contradicts an Uninstall row."""
    return "Uninstall" if _is_pdf_structure_installed() else "Install"


def _open_pdf_install_confirm(app: FNDApp) -> None:
    """Push the disclosure + Yes/Cancel confirm. On Yes the install (or
    uninstall) progress modal lands — wired in step 6b."""
    from fnd.tui.settings_screen import StructuredPdfConfirmScreen

    app.push_screen(StructuredPdfConfirmScreen())


def _get_indexer_auto_resume(app: FNDApp) -> bool:
    cfg = app._config  # type: ignore[attr-defined]
    return cfg.defaults.indexer_auto_resume if cfg is not None else True


def _get_cache_at_index_time(app: FNDApp) -> bool:
    cfg = app._config  # type: ignore[attr-defined]
    return cfg.defaults.cache_at_index_time if cfg is not None else True


def _summary_cache_update(app: FNDApp) -> str:
    """Trailing context for the Update cache action — number of PDFs
    that don't yet have a cache entry, with a rough time estimate.
    Lazy-loaded since it scans every source dir."""
    from fnd.tui.lazy_trailing import get_or_schedule

    def _compute() -> str:
        cfg = app._config  # type: ignore[attr-defined]
        if cfg is None:
            return ""
        try:
            from fnd.cache import ExtractionCache, default_cache_dir, sha256_file
            from fnd.extract.pdf import _extractor_signature
            from fnd.walk import walk_sources
        except Exception:
            return ""
        if not default_cache_dir().exists():
            n_missing = _count_pdfs_in_all_collections(cfg)
            return f"{n_missing} missing"
        cache = ExtractionCache()
        sig = _extractor_signature()
        n_missing = 0
        for coll in cfg.collections.values():
            for path in walk_sources(sources=list(coll.sources)):
                if path.suffix.lower() != ".pdf":
                    continue
                try:
                    sha = sha256_file(path)
                except OSError:
                    continue
                key = cache.build_key(content_sha256=sha, extractor_signature=sig)
                if not cache.entry_path(key).exists():
                    n_missing += 1
        if n_missing == 0:
            return "all cached"
        return f"{n_missing} missing"

    return get_or_schedule(app, "indexing.cache_update.missing", _compute)


def _count_pdfs_in_all_collections(cfg: Any) -> int:
    from fnd.walk import walk_sources

    n = 0
    for coll in cfg.collections.values():
        for path in walk_sources(sources=list(coll.sources)):
            if path.suffix.lower() == ".pdf":
                n += 1
    return n


def _run_update_cache(app: FNDApp) -> None:
    """Confirm + populate cache entries for all uncached PDFs.

    Doesn't touch the search index — runs the structuring pipeline only.
    The actual work is handled by a dedicated worker; this stub pushes
    the confirm screen which then triggers the run. Phase E wires the
    worker; for now we notify and bail so the menu plumbing works."""
    import contextlib

    with contextlib.suppress(Exception):
        app.notify(
            "Update cache action — worker wires up in Phase E. "
            "For now, use Update index from a collection.",
            timeout=5,
        )


def _summary_indexing(app: FNDApp) -> str:
    """Trailing summary for the Indexing root row.

    Auto-resume state reads instantly from config; cache size goes
    through the lazy-trailing cache so the fs walk doesn't block."""
    from fnd.tui.lazy_trailing import PLACEHOLDER, get_or_schedule

    auto = "✓ auto-resume" if _get_indexer_auto_resume(app) else "✗ auto-resume"
    cache_part = get_or_schedule(app, "indexing.summary.cache_short", _cache_size_short)
    if cache_part and cache_part != PLACEHOLDER:
        return f"{auto} · {cache_part}"
    return auto


def _summary_cache_size_row(app: FNDApp) -> str:
    """Trailing for the Cache size row inside Indexing.

    First call returns ``…`` while a worker thread walks the cache
    directory; the screen re-renders with the real value on completion.
    """
    from fnd.tui.lazy_trailing import get_or_schedule

    def _compute() -> str:
        from fnd.cache import ExtractionCache, default_cache_dir

        root = default_cache_dir()
        if not root.exists():
            return "empty"
        cache = ExtractionCache()
        return f"{cache.entry_count()} entries · {_human_bytes(cache.total_size_bytes())}"

    return get_or_schedule(app, "indexing.cache_size", _compute)


def _summary_cache_location_row(_app: FNDApp) -> str:
    """Trailing for the Cache location row — abbreviated home-dir path.

    Cheap (string ops only) so no lazy wrapper needed."""
    from fnd.cache import default_cache_dir

    p = str(default_cache_dir())
    home = str(Path.home())
    if p.startswith(home):
        p = "~" + p[len(home) :]
    if len(p) > 50:
        p = "…" + p[-50:]
    return p


def _cache_size_short() -> str:
    """Compact 'N · S' for the root summary; empty string when no cache."""
    from fnd.cache import ExtractionCache, default_cache_dir

    root = default_cache_dir()
    if not root.exists():
        return ""
    cache = ExtractionCache()
    n = cache.entry_count()
    if n == 0:
        return ""
    return f"cache {_human_bytes(cache.total_size_bytes())}"


def _human_bytes(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    kb = n / 1024
    if kb < 1024:
        return f"{kb:.0f} KB"
    mb = kb / 1024
    if mb < 1024:
        return f"{mb:.0f} MB"
    return f"{mb / 1024:.1f} GB"


def _summary_stale_entries(app: FNDApp) -> str:
    """Stale-entry count for the Prune row. Wrapped in lazy-trailing
    because it walks the cache directory."""
    from fnd.tui.lazy_trailing import get_or_schedule

    def _compute() -> str:
        from fnd.cache import default_cache_dir
        from fnd.extract.pdf import _extractor_signature

        root = default_cache_dir()
        if not root.exists():
            return "0 stale"
        current = _extractor_signature()
        stale = 0
        for shard in root.iterdir():
            if not shard.is_dir():
                continue
            for entry in shard.glob("*.json"):
                _, _, sig = entry.stem.partition("--")
                if sig != current:
                    stale += 1
        return f"{stale} stale"

    return get_or_schedule(app, "cache.stale_count", _compute)


def _run_cache_prune(app: FNDApp) -> None:
    """Count stale entries; confirm before deleting."""
    import contextlib

    from rich.text import Text

    from fnd.cache import default_cache_dir
    from fnd.extract.pdf import _extractor_signature
    from fnd.tui.settings_screen import CacheMaintenanceConfirm

    root = default_cache_dir()
    if not root.exists():
        with contextlib.suppress(Exception):
            app.notify("Cache is empty.")
        return
    current = _extractor_signature()
    stale: list[Path] = []
    fresh = 0
    for shard in root.iterdir():
        if not shard.is_dir():
            continue
        for entry in shard.glob("*.json"):
            _, _, sig = entry.stem.partition("--")
            if sig == current:
                fresh += 1
            else:
                stale.append(entry)
    if not stale:
        with contextlib.suppress(Exception):
            app.notify(f"No stale entries · {fresh} fresh.")
        return

    summary = Text()
    summary.append("Extractor signature: ", style="dim")
    summary.append(f"{current}\n", style="bold")
    summary.append("Fresh entries:  ", style="dim")
    summary.append(f"{fresh}\n", style="bold")
    summary.append("Stale entries:  ", style="dim")
    summary.append(str(len(stale)), style="bold")

    def _do_prune() -> int:
        removed = 0
        for p in stale:
            try:
                p.unlink()
                removed += 1
            except OSError:
                continue
        return removed

    app.push_screen(
        CacheMaintenanceConfirm(
            title="Indexing › Cache maintenance › Prune stale",
            summary=summary,
            run=_do_prune,
            confirm_label=f"Yes, remove {len(stale)} stale entries",
            result_label="stale entries removed",
            irreversible=False,
        )
    )


def _run_cache_clear(app: FNDApp) -> None:
    import contextlib

    from rich.text import Text

    from fnd.cache import ExtractionCache, default_cache_dir
    from fnd.tui.settings_screen import CacheMaintenanceConfirm

    root = default_cache_dir()
    if not root.exists():
        with contextlib.suppress(Exception):
            app.notify("Cache is empty.")
        return
    cache = ExtractionCache()
    n = cache.entry_count()
    size = cache.total_size_bytes()

    summary = Text()
    summary.append("Entries: ", style="dim")
    summary.append(f"{n}\n", style="bold")
    summary.append("Size:    ", style="dim")
    summary.append(f"{_human_bytes(size)}\n", style="bold")
    summary.append("Path:    ", style="dim")
    summary.append(f"{root}\n\n", style="bold")
    summary.append(
        "Next reindex will re-extract every PDF from scratch — "
        "structured extraction is ~30 s per PDF.",
        style="dim",
    )

    def _do_clear() -> int:
        import shutil

        shutil.rmtree(root, ignore_errors=True)
        return n

    app.push_screen(
        CacheMaintenanceConfirm(
            title="Indexing › Cache maintenance › Clear",
            summary=summary,
            run=_do_clear,
            confirm_label="Yes, clear PDF structure cache",
            result_label="entries removed",
            irreversible=True,
        )
    )


def _provider_root(_app: FNDApp) -> tuple[MenuItem, ...]:
    """Root settings menu — a clean, short list of categories. No
    content piled on top of each other. Each drill-in row pushes its
    own sub-screen."""
    return (
        MenuItem(
            id=f"root.{SECTION_PREFERENCES}",
            label="Preferences",
            description="Preferences: result limit, debounce, defaults, highlights, ranking.",
            kind=KIND_EXTERNAL,
            external=_open_section(SECTION_PREFERENCES),
            value_getter=_summary_preferences,
        ),
        MenuItem(
            id=f"root.{SECTION_COLLECTIONS}",
            label="Collections",
            description="Add, edit, or delete collections and their sources.",
            kind=KIND_EXTERNAL,
            external=_open_section(SECTION_COLLECTIONS),
            value_getter=_summary_collections,
        ),
        MenuItem(
            id=f"root.{SECTION_KEYBINDINGS}",
            label="Keybindings",
            description="Every key and what it does. Press a key in the list to invoke it.",
            kind=KIND_EXTERNAL,
            external=_open_section(SECTION_KEYBINDINGS),
            value_getter=_summary_keybindings,
        ),
        MenuItem(
            id=f"root.{SECTION_INDEXING}",
            label="Indexing",
            description=(
                "Structured-PDF extra, cache, and auto-resume behaviour — "
                "everything that shapes how reindex runs."
            ),
            kind=KIND_EXTERNAL,
            external=_open_section(SECTION_INDEXING),
            value_getter=_summary_indexing,
            keywords=("index", "indexer", "reindex", "pdf", "cache", "auto-resume"),
        ),
        header("External", level=2, anchor_id="external"),
        MenuItem(
            id="root.open_config_file",
            label="Config file",
            description="Open config.toml in $EDITOR; reload on save. Shift+⏎ reveals in Finder.",
            kind=KIND_EXTERNAL,
            external=_open_config_file_action,
            value_getter=_summary_config_path,
            external_app=True,
            keywords=("edit", "config", "toml", "open", "external"),
        ),
        MenuItem(
            id="root.open_keybindings_file",
            label="Keybindings file",
            description="Open keybindings.toml in $EDITOR. Shift+⏎ reveals in Finder.",
            kind=KIND_EXTERNAL,
            external=_open_keybindings_file_action,
            value_getter=_summary_keybindings_path,
            external_app=True,
            keywords=("edit", "keybindings", "rebind", "open", "external"),
        ),
    )


def build_root_items(app: FNDApp) -> tuple[MenuItem, ...]:
    """The rows the root :class:`SettingsScreen` renders."""
    return _provider_root(app)


# Section providers exposed by id — used by `open_settings_section` and
# the `?` shortcut to push a specific sub-screen directly.
_SECTION_PROVIDERS: dict[str, Callable[[FNDApp], tuple[MenuItem, ...]]] = {
    SECTION_PREFERENCES: _provider_preferences,
    SECTION_COLLECTIONS: _provider_collections,
    SECTION_KEYBINDINGS: _provider_keybindings,
    SECTION_INDEXING: _provider_indexing,
}

_SECTION_LABELS: dict[str, str] = {
    SECTION_PREFERENCES: "Preferences",
    SECTION_COLLECTIONS: "Collections",
    SECTION_KEYBINDINGS: "Keybindings",
    SECTION_INDEXING: "Indexing",
}


def section_items(
    app: FNDApp,
    section_id: str,
    *,
    context_hint: str | None = None,
) -> tuple[MenuItem, ...]:
    """Return the rows for a named sub-screen.

    ``context_hint`` is forwarded to providers that accept it
    (Keybindings uses it to surface the section relevant to the screen
    that opened it). Providers that don't expect the kwarg ignore it.
    """
    provider = _SECTION_PROVIDERS.get(section_id)
    if provider is None:
        return ()
    if section_id == SECTION_KEYBINDINGS:
        return _provider_keybindings(app, context_hint=context_hint)
    return provider(app)


def section_label(section_id: str) -> str:
    return _SECTION_LABELS.get(section_id, section_id)


def _pseudo_scope_row() -> MenuItem:
    """A search-only row that explains where the active-collection scope
    lives (sidebar in the main app, not the settings menu)."""
    return MenuItem(
        id="pseudo.scope",
        label="Active collection scope",
        description=(
            "Toggle which collections / sources are included in the "
            "current search scope from the main app's Collections sidebar "
            "(press `c` to focus it). Not a config setting."
        ),
        kind=KIND_ACTION,
        action_id="focus_collections_panel",
        keywords=("scope", "active", "toggle collection", "sidebar"),
    )


def walk_all_sections(app: FNDApp) -> Iterator[tuple[tuple[str, ...], MenuItem]]:
    """Yield ``(breadcrumb, leaf)`` pairs for every selectable item across
    every section. The basis for cross-section search.

    Headers are skipped. Per-collection sub-screens are NOT descended —
    finding a collection in search drills into its editor anyway.
    """
    for section_id, label in _SECTION_LABELS.items():
        breadcrumb = (label,)
        for item in section_items(app, section_id):
            if item.kind == KIND_HEADER:
                continue
            yield breadcrumb, item
    # Root-level actions that aren't behind a category drill.
    for item in build_root_items(app):
        if item.id in ("root.open_config_file", "root.open_keybindings_file"):
            yield (), item
    # Pseudo-rows surface confusions in search without taking up real estate
    # in any sub-screen.
    yield (), _pseudo_scope_row()
