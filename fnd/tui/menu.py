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

from fnd import os_labels
from fnd.config import ALL_COLLECTIONS, is_all_collections

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
SECTION_INDEXING_PDF_TEXTURE = "indexing-pdf-texture"
SECTION_FILTERS = "filters"
# Legacy aliases retained so any saved jump-state or external link that
# referenced the pre-combine section ids still routes into the combined
# screen instead of crashing.
SECTION_INDEXING = SECTION_INDEXING_PDF_TEXTURE
SECTION_PDF_TEXTURE = SECTION_INDEXING_PDF_TEXTURE


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
    # Render ``description`` as Rich markup (colour). Off by default so
    # arbitrary text (paths, globs, notes) shows literally; opt in only for
    # hand-authored descriptions that use ``[colour]…[/]`` tags.
    description_markup: bool = False
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
    # Takes precedence over ``setting_path``, for a row that edits screen-local
    # state rather than the config file.
    scalar_setter: Callable[[FNDApp, Any], None] | None = None

    # TOGGLE
    toggle_getter: Callable[[FNDApp], bool] | None = None
    toggle_setter: Callable[[FNDApp, bool], None] | None = None

    # PICKER
    multi: bool = False
    choices_provider: Callable[[FNDApp], list[ChoiceOption]] | None = None
    # TREE_PICKER: category→item model for the nested ToggleTree picker.
    groups_provider: Callable[[FNDApp], list[Any]] | None = None
    picker_getter: Callable[[FNDApp], Any] | None = None
    picker_setter: Callable[[FNDApp, Any], None] | None = None

    # EXTERNAL
    external: Callable[[FNDApp], None] | None = None
    # When True, the row launches an OS-level app ($EDITOR, file manager, etc.)
    # rather than pushing an internal Settings screen. Render leading
    # `↗` glyph; trailing slot carries the path (not a drill arrow).
    external_app: bool = False

    # Metadata used by the cross-tree search view.
    keywords: tuple[str, ...] = field(default_factory=tuple)

    # Renders contiguous items with the same subsection inside one
    # bordered Vertical with this string as its border_title. None =
    # outside any bordered group (the default for every existing item).
    subsection: str | None = None

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
    # A comma-separated ``default_key`` lists alternatives (same action, more
    # than one key) — render each and join them.
    if "," in key:
        return " / ".join(_pretty_key(k.strip()) for k in key.split(",") if k.strip())
    if key in _KEY_PRETTY:
        return _KEY_PRETTY[key]
    # ctrl+right → Ctrl+→; lone letters stay literal. Modifier spelling comes
    # from ``os_labels`` so alt renders ⌥ on macOS and Alt elsewhere.
    parts: list[str] = []
    for p in key.split("+"):
        modifier = os_labels.modifier_label(p)
        if modifier is not None:
            parts.append(modifier)
        elif p in _KEY_PRETTY:  # arrow / named keys keep their glyph
            parts.append(_KEY_PRETTY[p])
        else:
            parts.append(p.upper())
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
        "Open the focused row. Picker rows show their chooser; scalar rows open the inline edit bar; drill-in rows push a sub-screen.",
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
        "Filter rows across the current screen by label, key column, and keywords. Descriptions are excluded; they're advisory, not searchable.",
    ),
    (
        "1-9",
        "Jump by index",
        "",
        "Number keys jump the cursor straight to the nth visible row in the current section.",
    ),
    (
        "Shift+Enter",
        os_labels.REVEAL_LABEL,
        "",
        f"On file-pointing rows (config.toml, keybindings.toml, source paths) opens "
        f"{os_labels.FILE_MANAGER} with that file selected. No-op on other rows.",
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
        "Fire the highlighted (★) app: the one the resolver would have used for `o`. ★ shows which app fnd thinks is best for this hit.",
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
        "Close the modal. AX permission isn't required to use fnd; Preview will just open at page 1 without it.",
    ),
)


# Results-pane keys owned by ``ResultsTree`` widget bindings (not the action
# registry), so they're hand-curated here and appended to the Results section.
# A function, not a constant: the Apple-Terminal workaround is conditional
# content (a Terminal.app setting), not a word substitution.
def _keys_results_widget() -> tuple[tuple[str, str, str, str], ...]:
    skim_hint = (
        f"Hold {os_labels.ALT_WORD} and arrow through results to move the cursor "
        "WITHOUT loading each preview — browse fast with no mount or lag per row. "
        "The preview loads again on a normal ↑/↓ (the row you land on) or Enter "
        "(the exact row you skimmed to)."
    )
    if os_labels.is_macos():
        skim_hint += (
            " On Apple Terminal, enable Settings → Profiles → Keys → Left Option "
            "key → Esc+ for Option+arrow to reach fnd."
        )
    return (
        (
            f"{os_labels.ALT_KEY} ↑ / {os_labels.ALT_KEY} ↓",
            "Skim (no preview load)",
            "",
            skim_hint,
        ),
        (
            "Enter",
            "Load skimmed row",
            "",
            f"Load the highlighted result into the preview — handy right after an "
            f"{os_labels.ALT_WORD}-skim to mount exactly the row you stopped on, "
            f"without stepping.",
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
    # Slug the row id from the *un*localised key/label so ids stay identical on
    # every OS — "⌥ ↑" and "Alt ↑" must not mint two different ids for one row.
    item_id = f"key.{action_id}" if action_id else "key." + _slug(section, key, label)
    # Single localise seam for the whole cheat sheet: registry-derived rows and
    # the static widget tables both land here, so neither can drift into
    # hardcoded macOS vocabulary. ``key`` is localised too — the skim row's
    # modifier lives in the key column.
    key = os_labels.localise(key)
    return MenuItem(
        id=item_id,
        label=os_labels.localise(label),
        description=os_labels.localise(description),
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

    # Results-pane widget bindings (Option-skim, Enter-load) live on ResultsTree,
    # not the registry — append them to the registry-derived Results section.
    sections["Results pane"].extend(
        _key_row(*row, section="results_widget") for row in _keys_results_widget()
    )

    # Static widget bindings — append AFTER the registry-derived
    # sections in declaration order; reordering happens below.
    # Pass section name so widget-only rows that share labels across
    # sections (multiple "Cancel" rows) get distinct MenuItem ids.
    sections["Settings menu"] = [_key_row(*row, section="settings") for row in _KEYS_SETTINGS]
    sections["Source form"] = [_key_row(*row, section="source_form") for row in _KEYS_SOURCE_FORM]
    sections["Open with… modal"] = [_key_row(*row, section="open_with") for row in _KEYS_OPEN_WITH]
    # AX permission gates the macOS Preview AppleScript page-jump, so the modal
    # can never surface on Linux/Windows — listing its keys there would point
    # users at a System Settings pane their OS doesn't have.
    if os_labels.is_macos():
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
        app._search.ranking_profile = app._search.resolve_profile()  # type: ignore[attr-defined]
        app._refresh_status()  # type: ignore[attr-defined]

    return _set


def _coerce_str_list(raw: str) -> list[str]:
    """Comma-separated text -> list. Empty input clears the list."""
    return [part.strip() for part in raw.split(",") if part.strip()]


def _get_str_list_default(field_name: str) -> Callable[[FNDApp], str]:
    """Render a list-valued default as the comma-separated text the row edits."""

    def getter(app: FNDApp) -> str:
        cfg = app._config
        if cfg is None:
            return ""
        return ", ".join(getattr(cfg.defaults, field_name, []) or [])

    return getter


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
    if cfg is None:
        return ALL_COLLECTIONS
    want = cfg.defaults.collection
    # Normalise casing / an unknown name back onto a real choice so the
    # picker always shows a row as selected.
    if is_all_collections(want, known=set(cfg.collections)):
        return ALL_COLLECTIONS
    if want in cfg.collections:
        return want
    # Unknown name: fall back to whichever row the choices list actually
    # offers first, so the picker never highlights nothing.
    choices = _choices_collections(app)
    return choices[0].value if choices else ALL_COLLECTIONS


def _choices_collections(app: FNDApp) -> list[ChoiceOption]:
    cfg = app._config  # type: ignore[attr-defined]
    if cfg is None:
        return [ChoiceOption(value=ALL_COLLECTIONS, label="All collections")]
    names = sorted(cfg.collections)
    choices = [ChoiceOption(value=n, label=n) for n in names]
    # A collection literally named ``all`` predates the pseudo-name and wins
    # when the stored value is resolved. Offering the pseudo-choice too would
    # put two rows on the same stored value, and picking "All collections"
    # would silently select that one collection instead.
    if not any(n.casefold() == ALL_COLLECTIONS for n in names):
        choices.insert(0, ChoiceOption(value=ALL_COLLECTIONS, label="All collections"))
    return choices


def _choices_ranking(app: FNDApp) -> list[ChoiceOption]:
    cfg = app._config  # type: ignore[attr-defined]
    if cfg is None:
        return []
    return [ChoiceOption(value=n, label=n) for n in sorted(cfg.ranking)]


def _set_highlights(app: FNDApp, value: bool) -> None:
    if app._search.highlights_enabled != value:  # type: ignore[attr-defined]
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
            id="pref.preview_warm_margin",
            label="Warm context around matches",
            description=(
                "Chunks captured either side of a match when warming ahead, so "
                "a jump lands with context already built. 0 warms the matches "
                "alone and leaves the gaps to scroll. Higher values warm more "
                "of each file and fewer files per second."
            ),
            kind=KIND_SCALAR,
            setting_path="defaults.preview_warm_margin",
            hint="0-20",
            coerce=int,
            value_getter=_get_int_default("preview_warm_margin", 2),
            keywords=("warm", "margin", "context", "preview", "cache", "ahead"),
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
            toggle_getter=lambda app: app._search.highlights_enabled,  # type: ignore[attr-defined]
            toggle_setter=_set_highlights,
            keywords=("highlight",),
        ),
        MenuItem(
            id="pref.multicolour_highlights",
            label="Multi-colour highlights",
            description=(
                "Give each word in a multi-word query its own highlight colour. "
                "When off, all matches use one colour. Applies on the next search."
            ),
            kind=KIND_TOGGLE,
            toggle_getter=lambda app: (  # type: ignore[arg-type]
                app._config.defaults.multicolour_highlights  # type: ignore[attr-defined]
                if app._config  # type: ignore[attr-defined]
                else True
            ),
            toggle_setter=lambda app, v: _setting_writer("defaults.multicolour_highlights")(app, v),
            keywords=("highlight", "colour", "color", "multi", "rainbow", "term"),
        ),
        MenuItem(
            id="pref.scrollbar_match_highlight",
            label="Scrollbar match markers (in development)",
            description=(
                "Mark match positions on the preview scrollbar. In development: "
                "accurate for PDF/text and small markdown; large markdown lazy-mounts "
                "a chunk window, so its markers drift. Applies on next preview load."
            ),
            kind=KIND_TOGGLE,
            toggle_getter=lambda app: (  # type: ignore[arg-type]
                app._config.defaults.scrollbar_match_highlight  # type: ignore[attr-defined]
                if app._config  # type: ignore[attr-defined]
                else False
            ),
            toggle_setter=lambda app, v: _setting_writer("defaults.scrollbar_match_highlight")(
                app, v
            ),
            keywords=("scrollbar", "marker", "match", "highlight", "position"),
        ),
        MenuItem(
            id="pref.preview_scroll_animation",
            label="Glide to matches",
            description=(
                "Glide the preview to a match inside the file already on screen, "
                "instead of cutting to it. Off makes every landing an instant jump — "
                "also the way to tell a mislanding from the glide passing over it."
            ),
            kind=KIND_TOGGLE,
            toggle_getter=lambda app: (  # type: ignore[arg-type]
                app._config.defaults.preview_scroll_animation  # type: ignore[attr-defined]
                if app._config  # type: ignore[attr-defined]
                else True
            ),
            toggle_setter=lambda app, v: _setting_writer("defaults.preview_scroll_animation")(
                app, v
            ),
            keywords=("scroll", "animation", "glide", "slide", "smooth", "jump", "motion"),
        ),
        MenuItem(
            id="pref.render_mermaid",
            label="Render mermaid diagrams (in development)",
            description=(
                "Render ```mermaid code fences as terminal text-art diagrams "
                "instead of source. Unsupported or oversized diagrams fall back "
                "to source. Applies on next preview load."
            ),
            kind=KIND_TOGGLE,
            toggle_getter=lambda app: (  # type: ignore[arg-type]
                app._config.defaults.render_mermaid  # type: ignore[attr-defined]
                if app._config  # type: ignore[attr-defined]
                else True  # default-on: reflect the model default when config absent
            ),
            toggle_setter=lambda app, v: _setting_writer("defaults.render_mermaid")(app, v),
            keywords=("mermaid", "diagram", "flowchart", "render", "fence"),
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
            description=(
                "Scope a fresh profile starts with — All collections, or one "
                "named collection. Your sidebar selection is remembered and "
                "wins once you've made one, so changing this only affects a "
                "profile that has never saved a scope. Use `-c all` (or "
                "`-c <name>`) to scope a single launch."
            ),
            kind=KIND_PICKER,
            choices_provider=_choices_collections,
            picker_getter=_get_default_collection,
            picker_setter=_setting_writer("defaults.collection"),
            keywords=("default", "collection", "all", "scope"),
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
            description="Let the resolver pick: skim → preview-if-AX → system for PDFs; system for others.",
        )
    ]
    for app_id, app_def in registry.items():
        if kind not in app_def.handles and "*" not in app_def.handles:
            continue
        if not app_def.available():
            continue
        # `reveal` acts on the file without opening it — offering it here would
        # let a default silently stop `o` from opening this kind. It stays in
        # the Open-with picker, which is a one-shot choice.
        if not app_def.selectable_default:
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
    active = "●" if name in (app._scope.collections or []) else "○"  # type: ignore[attr-defined]
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
        app._indexer.reindex_with_warning(name)  # type: ignore[attr-defined]

    return _run


def _make_texturise_flat(name: str) -> Callable[[FNDApp], None]:
    """Per-collection action: re-run Update with texturising forced on.
    The cache short-circuits already-textured PDFs so the net cost is
    one texturising pass per still-flat PDF in the collection."""

    def _run(app: FNDApp) -> None:
        app._indexer.reindex_with_warning(  # type: ignore[attr-defined]
            name, texturise_override=True
        )

    return _run


def _make_rebuild(name: str) -> Callable[[FNDApp], None]:
    """Per-collection action: drop this collection's chunks and re-extract
    every file fresh, re-texturising every PDF under the current engine
    (cache bypassed). The deliberate, costly redo."""

    def _run(app: FNDApp) -> None:
        app._indexer.reindex_with_warning(  # type: ignore[attr-defined]
            name,
            texturise_override=True,
            skip_unchanged=False,
            force_fresh=True,
            rebuild=True,
        )

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
                "Open the new-collection wizard: pick a name, then add "
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
                "Per-file rules match the per-collection Update index: "
                "unchanged files are skipped, the PDF Texture Cache "
                "is consulted (not cleared)."
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


def _summary_collection_update(app: FNDApp, name: str) -> str:
    """Trailing context on the per-collection Update row. Counts the
    sources configured for the collection, plus an ETA based on the
    calibrated per-PDF cost when pdf-structure is installed."""
    cfg = app._config  # type: ignore[attr-defined]
    if cfg is None or name not in cfg.collections:
        return ""
    n_sources = len(cfg.collections[name].sources)
    return f"{n_sources} sources"


def _summary_flat_pdfs(app: FNDApp, name: str) -> str:
    """Lazy trailing: count of PDFs in this collection's sources that
    are NOT textured in the index (no body_struct on any chunk)."""
    from fnd.tui.lazy_trailing import get_or_schedule

    def _compute() -> str:
        return _compute_flat_pdfs_for(name)

    return get_or_schedule(app, f"col.{name}.flat_pdfs", _compute)


def _compute_flat_pdfs_for(name: str) -> str:
    import contextlib

    try:
        from pathlib import Path

        import tantivy

        from fnd.config import default_index_dir, load
        from fnd.schema import F_BODY_MD, F_KIND, F_PATH

        cfg = load()
        col = cfg.collections.get(name)
        if col is None:
            return ""
        on_disk: set[str] = set()
        for src in col.sources:
            root = Path(src.path).expanduser()
            if not root.exists():
                continue
            for p in root.rglob("*.pdf"):
                if p.is_file():
                    on_disk.add(str(p.resolve()))
        if not on_disk:
            return "no PDFs"
        index_dir = default_index_dir()
        if not index_dir.exists():
            return f"{len(on_disk)} flat PDFs"
        index = tantivy.Index.open(str(index_dir))
        index.reload()
        searcher = index.searcher()
        kind_q = tantivy.Query.term_query(index.schema, F_KIND, "pdf")
        textured: set[str] = set()
        for _score, addr in searcher.search(kind_q, limit=200000).hits:
            doc = searcher.doc(addr)
            if not doc.get_first(F_BODY_MD):  # type: ignore[attr-defined]
                continue
            path = doc.get_first(F_PATH)  # type: ignore[attr-defined]
            if path:
                with contextlib.suppress(OSError):
                    textured.add(str(Path(str(path)).resolve()))
        flat = len(on_disk - textured)
        if flat == 0:
            return "all textured"
        return f"{flat} flat PDF{'s' if flat != 1 else ''}"
    except Exception:
        return ""


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
    """Push the chain confirm dialog with the toggle honoured (the
    historical action; kept for the Update-all entries that don't
    explicitly override texturising)."""
    _push_update_all_confirm(app, texturise_override=None)


def _run_update_all_index_and_texturise(app: FNDApp) -> None:
    """Shared top-of-screen action: always texturises, ignoring toggle."""
    _push_update_all_confirm(app, texturise_override=True)


def _run_update_all_index_only(app: FNDApp) -> None:
    """Indexing-section action: always skips texturising, ignoring toggle."""
    _push_update_all_confirm(app, texturise_override=False)


def _run_rebuild_all_collections(app: FNDApp) -> None:
    """Rebuild every collection: drop chunks and re-extract every file
    fresh, re-texturising every PDF under the current engine (cache
    bypassed). The deliberate, costly redo across all collections."""
    _push_update_all_confirm(
        app, texturise_override=True, skip_unchanged=False, force_fresh=True, rebuild=True
    )


def _push_update_all_confirm(
    app: FNDApp,
    *,
    texturise_override: bool | None,
    skip_unchanged: bool = True,
    force_fresh: bool = False,
    rebuild: bool = False,
) -> None:
    """Shared helper: push the chain confirm dialog wired with the
    appropriate run mode."""
    import contextlib

    from fnd.tui.settings_screen import UpdateAllConfirm

    cfg = app._config  # type: ignore[attr-defined]
    if cfg is None or not cfg.collections:
        with contextlib.suppress(Exception):
            app.notify("No collections to update.")
        return
    names = sorted(cfg.collections.keys())
    app.push_screen(
        UpdateAllConfirm(
            collection_names=names,
            texturise_override=texturise_override,
            skip_unchanged=skip_unchanged,
            force_fresh=force_fresh,
            rebuild=rebuild,
        )
    )


def _provider_collection(app: FNDApp, name: str) -> tuple[MenuItem, ...]:
    """Per-collection sub-menu."""
    return (
        MenuItem(
            id=f"col.{name}.rename",
            label="Rename",
            description=(
                "Change this collection's name. Search scope and saved panel "
                "state follow the new name; the index is not rebuilt."
            ),
            kind=KIND_EXTERNAL,
            external=_make_open_rename(name),
        ),
        MenuItem(
            id=f"col.{name}.sources",
            label="Sources",
            description=(
                "The folders this collection indexes, and each one's filters, "
                "excludes and opening app."
            ),
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
            description=(
                "Which [ranking.<name>] block scores this collection's results "
                "— recency, file-type and phrase-proximity weights. Applies to "
                "the next search; no reindex."
            ),
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
            label="Update index",
            description=(
                "Add new / changed files and drop deleted ones; unchanged files "
                "are skipped and their existing texturing is left untouched. The "
                "cheap, battery-friendly pass — it never re-texturises what's "
                "already done."
            ),
            kind=KIND_ACTION,
            action_label="Update",
            external=_make_reindex(name),
            value_getter=(lambda n: lambda a: _summary_collection_update(a, n))(name),
        ),
        MenuItem(
            id=f"col.{name}.rebuild",
            label="Rebuild index (re-texturise all)",
            description=(
                "Drop this collection's chunks and re-extract every file from "
                "scratch, re-texturising every PDF under the current engine "
                "(cache bypassed). The deliberate, costly redo — use after an "
                "engine upgrade or to refresh every preview."
            ),
            kind=KIND_ACTION,
            action_label="Rebuild",
            external=_make_rebuild(name),
            keywords=("rebuild", "re-texturise", "retexturise", "refresh", "fresh", "redo"),
        ),
        MenuItem(
            id=f"col.{name}.texturise_flat",
            label="Texturise flat PDFs",
            description=(
                "Re-run Update index for this collection with texturising "
                "forced on, regardless of the Texturise-while-indexing "
                "toggle. The PDF Texture Cache skips already-textured "
                "PDFs, so the effective cost is one texturising pass per "
                "still-flat PDF."
            ),
            kind=KIND_ACTION,
            action_label="Run",
            external=_make_texturise_flat(name),
            value_getter=(lambda n: lambda a: _summary_flat_pdfs(a, n))(name),
            keywords=("texturise", "flat", "pdf", "retry", "engine"),
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
    app._search.ranking_profile = app._search.resolve_profile()  # type: ignore[attr-defined]
    app._refresh_status()  # type: ignore[attr-defined]


def _source_trailing(collection_name: str, idx: int) -> Callable[[FNDApp], str]:
    """Build a value_getter for a per-source row that shows file-types
    and a path-not-found warning when the source directory is missing."""

    def _summary(app: FNDApp) -> str:
        cfg = app._config  # type: ignore[attr-defined]
        if cfg is None or collection_name not in cfg.collections:
            return ""
        sources = cfg.collections[collection_name].sources
        if idx >= len(sources):
            return ""
        src = sources[idx]
        # The file types are the filter set's, not the include globs': those
        # are folded into ``filters.kinds`` when the config loads.
        from fnd.kinds import KIND_BY_ID

        kinds = list(src.effective_filters.kinds)
        if not kinds and src.includes:
            # Globs that name a suffix restrict the types just as kinds do,
            # even when they are not a complete set and so were not folded in.
            suffixes = {
                sfx
                for glob in src.includes
                for sfx in (glob[glob.rfind(".") :],)
                if glob.rfind(".") != -1
            }
            kinds = sorted({k for k, spec in KIND_BY_ID.items() if set(spec.suffixes) & suffixes})
        types = ", ".join(kinds) if kinds else "All types"
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
            description=(
                "Add a folder to this collection. Its files enter the index on the next update."
            ),
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
    """Indexing sub-screen — app-wide search-index actions and behaviour.

    The combined screen prepends an "Update everything (index +
    texturise)" action at the top via ``_provider_indexing_pdf_texture``;
    this provider supplies the Indexing-section-only items."""
    return (
        header("Status", level=2),
        MenuItem(
            id="indexing.files_in_index",
            label="Files in index",
            description=(
                "Distinct files (md, pptx, docx, txt, PDFs) that have at "
                "least one chunk in the search index, totalled across "
                "every collection. Updates the next time you open this "
                "screen after an Update index run."
            ),
            kind=KIND_DISPLAY,
            value_getter=_summary_files_in_index,
            keywords=("files", "index", "count", "indexed", "total"),
        ),
        header("Actions", level=2),
        MenuItem(
            id="indexing.update_all_index_only",
            label="Process new files (index only, no texturising)",
            description=(
                "Run Update index for every collection in sequence, "
                "indexing new and changed files (md, pptx, docx, txt, PDFs) "
                "but SKIPPING texturising for this run regardless of the "
                "Texturise-while-indexing toggle. Incremental: unchanged "
                "files are skipped, so this is a fast catch-up; texturise later."
            ),
            kind=KIND_ACTION,
            action_label="Run",
            external=_run_update_all_index_only,
            value_getter=_summary_update_all,
            keywords=(
                "process",
                "new",
                "update",
                "all",
                "index",
                "reindex",
                "everything",
                "skip",
                "texturise",
                "fast",
            ),
        ),
        header("Behaviour", level=2),
        MenuItem(
            id="indexing.auto_resume",
            label="Auto-resume on launch",
            description=(
                "✗ Off (default): indexing only ever runs when you trigger "
                "it, so a laptop never burns battery on work it didn't ask "
                "for. A manual Update index still resumes where a quit left "
                "off, skipping files already indexed. "
                "✓ On: an interrupted Update index (force-quit, sleep, Ctrl+C) "
                "resumes silently in the background next launch — progress "
                "shows in the footer, not a modal."
            ),
            kind=KIND_TOGGLE,
            toggle_getter=_get_indexer_auto_resume,
            toggle_setter=lambda app, v: _setting_writer("defaults.indexer_auto_resume")(app, v),
            setting_path="defaults.indexer_auto_resume",
            keywords=("auto", "resume", "indexer", "interrupted", "launch", "reindex"),
        ),
    )


def _provider_pdf_texture(_app: FNDApp) -> tuple[MenuItem, ...]:
    """PDF Texture sub-screen — engine, status / actions, cache, and
    texturising-while-indexing behaviour.

    Texturising is the act of turning a PDF's pages into structured
    Markdown (headings, lists, tables) for the preview pane. It does
    NOT affect search behaviour - PDFs are always searchable; a flat
    PDF is just a PDF whose preview is plain text rather than
    formatted."""
    return (
        header("Engine", level=2),
        MenuItem(
            id="pdf_texture.engine_status",
            label="Texturising engine",
            description=(
                "Whether the texturising engine is installed. When installed, "
                "the next Update index texturises any PDF that isn't already "
                "textured. When not installed, every PDF stays flat in the "
                "preview pane (search still works either way)."
            ),
            kind=KIND_DISPLAY,
            value_getter=_summary_pdf_status,
            keywords=(
                "pdf",
                "texturise",
                "texture",
                "engine",
                "status",
                "installed",
                # Legacy terms so existing user muscle memory still finds
                # this row.
                "structured",
                "structure",
                "pdf-structure",
                "extra",
            ),
        ),
        MenuItem(
            id="pdf_texture.install",
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
                "texturise",
                "texture",
                "engine",
                "install",
                "uninstall",
                "pymupdf4llm",
                "docling",
                # Legacy terms.
                "structured",
                "structure",
                "pdf-structure",
                "extra",
            ),
        ),
        header("Status / actions", level=2),
        MenuItem(
            id="pdf_texture.textured_count",
            label="PDFs textured",
            description=(
                "Distinct PDFs in your collections whose chunks have a "
                "non-empty body_md (rendered structurally in the preview "
                "pane). The Y total is every PDF the indexer can see on "
                "disk under your collection sources; ⚠ Z still flat = "
                "Y - X. Enter to drill into the list of still-flat PDFs "
                "with the reason per file and a Retry-per-file action."
            ),
            kind=KIND_EXTERNAL,
            external=_open_still_flat_drill,
            value_getter=_summary_pdfs_textured,
            keywords=("pdf", "textured", "flat", "count", "status", "drill"),
        ),
        MenuItem(
            id="pdf_texture.update",
            label="Texturise PDFs that are still flat",
            description=(
                "Run an Update-index pass with texturising forced on, "
                "re-attempting every PDF that is still flat (no structured "
                "preview). Already-textured PDFs are reused, not redone; "
                "unchanged non-PDFs are skipped. Use to fill in previews "
                "without a full rebuild."
            ),
            kind=KIND_ACTION,
            action_label="Run",
            external=_run_update_cache,
            value_getter=_summary_cache_update,
            keywords=("cache", "texture", "texturise", "flat", "warm", "populate"),
        ),
        MenuItem(
            id="pdf_texture.rebuild_all",
            label="Rebuild all collections (re-texturise everything)",
            description=(
                "Drop every collection's chunks and re-extract all files from "
                "scratch, re-texturising every PDF under the current engine "
                "(cache bypassed). The deliberate, costly redo across all "
                "collections — use after an engine upgrade or to refresh every "
                "preview. Searchable text is unchanged; only the preview "
                "rendering improves."
            ),
            kind=KIND_ACTION,
            action_label="Rebuild",
            external=_run_rebuild_all_collections,
            value_getter=_summary_rebuild_all,
            keywords=(
                "rebuild",
                "retexturise",
                "re-texturise",
                "outdated",
                "older",
                "upgrade",
                "version",
                "refresh",
                "everything",
                "all",
            ),
        ),
        header("Cache", level=2),
        MenuItem(
            id="pdf_texture.cache_size",
            label="Saved texturings",
            description=(
                "Per-file texturing results fnd has saved. Shared across "
                "collections; the same PDF in two collections is texturised "
                "once and reused. Clearing the cache only frees disk — your "
                "built previews keep working."
            ),
            kind=KIND_DISPLAY,
            value_getter=_summary_cache_size_row,
            keywords=("cache", "texture", "saved", "texturings", "size"),
        ),
        MenuItem(
            id="pdf_texture.cache_location",
            label="Location",
            description=(
                "Disk path. Safe to delete from outside fnd; the next Update "
                "index will re-create the directory as needed."
            ),
            kind=KIND_DISPLAY,
            value_getter=_summary_cache_location_row,
            keywords=("cache", "texture", "location", "path", "disk"),
        ),
        MenuItem(
            id="pdf_texture.cache_clear",
            label="Clear texture cache",
            description=(
                "Delete the saved texturings from disk to reclaim space. The "
                "previews you've already built keep working (the texturing is "
                "stored in the index, not the cache); the cache just speeds up "
                "future re-indexing. Rebuild re-creates entries as needed."
            ),
            kind=KIND_ACTION,
            action_label="Clear…",
            external=_run_cache_clear,
            keywords=(
                "cache",
                "texture",
                "clear",
                "free",
                "space",
                "disk",
                "delete",
                "reclaim",
                # Legacy term so muscle memory still finds this row.
                "leftover",
            ),
        ),
        MenuItem(
            id="pdf_texture.prune_orphans",
            label="Remove orphaned texturings",
            description=(
                "Delete saved texturings for files no longer on disk — removed, "
                "renamed, or de-configured. The cache is shared across "
                "collections and content-addressed, so a per-collection Rebuild "
                "can't reach these; this frees their space without disturbing "
                "live texturings. Scans every source to find what's still live."
            ),
            kind=KIND_ACTION,
            action_label="Remove…",
            external=_run_prune_orphans,
            keywords=(
                "orphan",
                "orphaned",
                "dead",
                "stale",
                "removed",
                "prune",
                "cache",
                "texture",
            ),
        ),
        header("Behaviour", level=2),
        MenuItem(
            id="pdf_texture.texturise_while_indexing",
            label="Texturise PDFs while indexing",
            description=(
                "✓ On (default when the texturising engine is installed): "
                "Update index texturises new PDFs as it goes. ✗ Off: Update "
                "index reuses saved texturings if they exist but skips "
                "texturising new PDFs. Fast flat-only refresh, useful when "
                "you're on battery or don't have CPU to spare."
            ),
            kind=KIND_TOGGLE,
            toggle_getter=_get_cache_at_index_time,
            toggle_setter=lambda app, v: _setting_writer("defaults.cache_at_index_time")(app, v),
            setting_path="defaults.cache_at_index_time",
            keywords=("cache", "texture", "texturise", "index", "battery", "fast"),
        ),
    )


def _provider_indexing_pdf_texture(app: FNDApp) -> tuple[MenuItem, ...]:
    """Combined screen: shared "Update everything" action at the top
    (subsection=None), then two bordered subsections grouping the
    Indexing and PDF Texture items respectively. The contributing
    providers' items are reused verbatim; only the ``subsection`` field
    is stamped on them."""
    import dataclasses as _dc

    shared = (
        MenuItem(
            id="indexing.update_all_index_and_texturise",
            label="Update every collection (index + texturise)",
            description=(
                "Run Update index for every collection in sequence, "
                "indexing new/changed files and ALWAYS texturising PDFs it "
                "processes, regardless of the Texturise-while-indexing toggle "
                "below. Incremental: unchanged, already-textured files are "
                "skipped; still-flat PDFs get texturised. Existing texturising "
                "is reused (never redone) across app updates."
            ),
            kind=KIND_ACTION,
            action_label="Run",
            external=_run_update_all_index_and_texturise,
            value_getter=_summary_update_all,
            keywords=(
                "update",
                "all",
                "everything",
                "process",
                "new",
                "index",
                "reindex",
                "texturise",
                "textured",
            ),
        ),
    )
    indexing_items = tuple(
        _dc.replace(item, subsection="Indexing") for item in _provider_indexing(app)
    )
    pdf_texture_items = tuple(
        _dc.replace(item, subsection="PDF Texture") for item in _provider_pdf_texture(app)
    )
    return shared + indexing_items + pdf_texture_items


# ── Index filters ────────────────────────────────────────────────────


def _filters_defaults(app: FNDApp) -> Any:
    """``[defaults.filters]``, or the shipped defaults under a config-less stub."""
    from fnd.config import DefaultFilters

    cfg = app._config  # type: ignore[attr-defined]
    return cfg.defaults.filters if cfg else DefaultFilters()


def _open_filter_browser(app: FNDApp) -> None:
    """The defaults, as branches rather than a column of text boxes."""
    from fnd.config import default_config_path, load, write_setting
    from fnd.tui.settings_screen import (
        FilterBrowserScreen,
        _spec_from_filters,
        _spec_to_mapping,
    )

    current = _filters_defaults(app)

    def _save(spec: Any, gitignore: bool, fndignore: bool) -> None:
        values = _spec_to_mapping(spec)
        values["respect_gitignore"] = gitignore
        values["respect_fndignore"] = fndignore
        for name, value in values.items():
            write_setting(
                config_path=default_config_path(),
                dotted_path=f"defaults.filters.{name}",
                # An empty list is written, not deleted: deleting the key lets
                # the model default (exclude_tags = ["no_index"]) come back,
                # so "clear all" silently reinstated an exclusion.
                value=None if value == "" else value,
            )
        app._config = load()  # type: ignore[attr-defined]
        app._refresh_status()  # type: ignore[attr-defined]

    app.push_screen(
        FilterBrowserScreen(
            title="Index filters",
            spec=_spec_from_filters(current),
            gitignore=current.respect_gitignore,
            fndignore=current.respect_fndignore,
            sample_provider=lambda: _sample_first_source(app),
            on_save=_save,
        )
    )


def _sample_first_source(app: FNDApp) -> Any:
    """Values seen in the configured sources, for the pickers to offer.

    Bounded: a picker wants suggestions, not an inventory, and a cloud-backed
    folder must not stall the screen opening.
    """
    from pathlib import Path

    from fnd.filters.scan import SourceSample, sample_source

    cfg = app._config  # type: ignore[attr-defined]
    if cfg is None:
        return None
    merged = SourceSample()
    for collection in list(cfg.collections.values())[:3]:
        for source in collection.sources[:2]:
            root = Path(source.path)
            if not root.exists():
                continue
            part = sample_source(root, budget_s=0.6)
            merged.files_seen += part.files_seen
            merged.truncated = merged.truncated or part.truncated
            for kind, n in part.kinds.items():
                merged.kinds[kind] = merged.kinds.get(kind, 0) + n
            for src, values in part.tags.items():
                bucket = merged.tags.setdefault(src, {})
                for value, n in values.items():
                    bucket[value] = bucket.get(value, 0) + n
    return merged


def _summary_index_filters(app: FNDApp) -> str:
    f = _filters_defaults(app)
    bits: list[str] = []
    on = [
        n
        for n, v in ((".gitignore", f.respect_gitignore), (".fndignore", f.respect_fndignore))
        if v
    ]
    if on:
        bits.append(", ".join(on))
    from fnd.filters.dimensions import tag_selection

    dropped = sorted({t for tags in tag_selection(f.exclude_tags).values() for t in tags})
    if dropped:
        bits.append(
            f"never {'/'.join(dropped)}" if len(dropped) < 3 else f"never {len(dropped)} tags"
        )
    kept = sorted({t for tags in tag_selection(f.include_tags).values() for t in tags})
    if kept:
        bits.append(f"only {'/'.join(kept)}" if len(kept) < 3 else f"only {len(kept)} tags")
    if f.kinds:
        bits.append(f"{len(f.kinds)} types")
    if f.max_size or f.min_size:
        bits.append("size")
    if f.created_after or f.modified_after:
        bits.append("dates")
    if f.frontmatter or f.expression:
        bits.append("custom")
    return " · ".join(bits) if bits else "off"


def _provider_index_filters(_app: FNDApp) -> tuple[MenuItem, ...]:
    """One row into the filter browser, rather than a column of typed fields."""
    return (
        MenuItem(
            id="filters.browse",
            label="Index filters",
            description=(
                "Which files enter the index: file types, tags, size, dates "
                "and ignore files, as branches you tick — or as one expression "
                "if you prefer. Needs a reindex to take effect."
            ),
            kind=KIND_EXTERNAL,
            external=_open_filter_browser,
            value_getter=_summary_index_filters,
            keywords=(
                "filter",
                "filters",
                "ignore",
                "gitignore",
                "fndignore",
                "tag",
                "no_index",
                "kind",
                "type",
                "size",
                "date",
                "exclude",
                "skip",
            ),
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
    return (
        "Uninstall texturising engine"
        if _is_pdf_structure_installed()
        else "Install texturising engine"
    )


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
    return cfg.defaults.indexer_auto_resume if cfg is not None else False


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
            from fnd.extract.pdf import texture_signature
            from fnd.walk import walk_sources
        except Exception:
            return ""
        if not default_cache_dir().exists():
            n_missing = _count_pdfs_in_all_collections(cfg)
            return f"{n_missing} missing"
        cache = ExtractionCache()
        sig = texture_signature()
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
    """Texturise every still-flat PDF across every collection.

    The cache stores extraction RESULTS (chunks), not extraction MODE.
    A PDF cached with empty body_md (older entries, runs before docling
    was installed, runs where docling timed out) will cache-hit on
    every subsequent Update and never get re-attempted - the whole
    point of this action is to force a fresh extraction pass on those
    files. So: forget the cache entry for each still-flat PDF first,
    THEN run the Update-all chain with texturise forced on. Cached
    already-textured PDFs are left alone and short-circuit normally."""
    # The flat-PDF scan + per-file sha/cache-forget is seconds-long on a
    # real corpus; running it in the menu handler froze the TUI before the
    # confirm appeared. Do it on a worker thread, then push the confirm
    # back on the UI thread. The menu stays responsive throughout.
    import asyncio
    import contextlib
    import threading

    def _worker() -> None:
        from fnd.tui import flat_pdf_scan

        # Only flag the wait when the scan is actually cold (seconds);
        # a warm cache makes the confirm appear instantly, no toast needed.
        if not flat_pdf_scan.is_fresh(None):
            with contextlib.suppress(Exception):
                app.call_from_thread(
                    app.notify, "Scanning flat PDFs and preparing update…", timeout=3
                )
        # Forgetting cache entries is a best-effort optimisation; a failure
        # here (e.g. the cold recompute hitting a transient index lock) must
        # not kill the worker before the confirm is pushed, or the click
        # would silently do nothing. The texturise-all run still proceeds.
        with contextlib.suppress(Exception):
            _forget_cache_for_flat_pdfs()
        with contextlib.suppress(Exception):
            result = app.call_from_thread(_push_update_all_confirm, app, texturise_override=True)
            if asyncio.iscoroutine(result):
                result.close()

    threading.Thread(target=_worker, daemon=True).start()


def _forget_cache_for_flat_pdfs() -> None:
    """Delete the structured-extraction cache entry for every PDF
    that is currently flat in the index. Best-effort - any per-file
    failure (sha read, cache write) is suppressed so a single bad
    file doesn't take down the whole texturise run.

    Reuses the most recent background scan when one is cached; otherwise
    computes inline (this runs on a worker thread, never the event loop —
    see ``_run_update_cache``)."""
    import contextlib

    try:
        from fnd.cache import ExtractionCache, sha256_file
        from fnd.extract.pdf import texture_signature
        from fnd.tui import flat_pdf_scan
        from fnd.tui.settings_screen import _flat_pdfs_with_reasons
    except Exception:
        return
    # Require a FRESH snapshot: a stale one could miss a newly-flat PDF and
    # leave it un-forgotten, so it cache-hits and stays flat through the very
    # run meant to fix it. Recompute (off-loop here) when stale.
    rows = flat_pdf_scan.cached_rows(None) if flat_pdf_scan.is_fresh(None) else None
    if rows is None:
        try:
            rows = list(_flat_pdfs_with_reasons())
        except Exception:
            # Transient scan failure (e.g. index locked mid-rebuild): forget
            # what the last snapshot knew about rather than aborting entirely.
            rows = flat_pdf_scan.cached_rows(None) or []
    cache = ExtractionCache()
    sig = texture_signature()
    for _collection, path, _reason, _recorded_at in rows:
        with contextlib.suppress(OSError):
            from pathlib import Path

            sha = sha256_file(Path(path))
            key = cache.build_key(content_sha256=sha, extractor_signature=sig)
            entry = cache.entry_path(key)
            if entry.exists():
                with contextlib.suppress(OSError):
                    entry.unlink()


def _summary_indexing(app: FNDApp) -> str:
    """Auto-resume chip — still used by tests after the section combine."""
    return "✓ auto-resume" if _get_indexer_auto_resume(app) else "✗ auto-resume"


def _engine_chip() -> str:
    """Engine state for the trailing summary.

    Off the render path with its two siblings: it stats the uv tool root and
    calls ``importlib.invalidate_caches()``, whose cost is process-wide and
    lands on whatever imports next rather than showing up here.
    """
    return "✓ engine on" if _is_pdf_structure_installed() else "✗ engine off"


def _summary_pdf_texture(app: FNDApp) -> str:
    """Engine + cache chip — still used by tests after the section combine."""
    from fnd.tui.lazy_trailing import PLACEHOLDER, get_or_schedule

    parts: list[str] = []
    engine = get_or_schedule(app, "pdf_texture.summary.engine", _engine_chip)
    if engine and engine != PLACEHOLDER:
        parts.append(engine)
    cache_part = get_or_schedule(app, "pdf_texture.summary.cache_short", _cache_size_short)
    if cache_part and cache_part != PLACEHOLDER:
        parts.append(cache_part)
    stale_part = get_or_schedule(app, "pdf_texture.summary.stale_short", _stale_count_short)
    if stale_part and stale_part != PLACEHOLDER:
        parts.append(stale_part)
    return " · ".join(parts)


def _structured_engine_active() -> bool:
    """Whether the current extractor actually produces structured output
    (pymupdf4llm importable). Distinct from _is_pdf_structure_installed(),
    which tracks the in-app install marker — re-texturisability depends on
    the live capability that extractor_signature() encodes, so re-texturise
    counts gate on this. A 'flat' signature means re-texturising would only
    re-produce flat output, so there's nothing to offer."""
    from fnd.extract.pdf import extractor_signature

    return not extractor_signature().startswith("flat")


def _stale_count_short() -> str:
    """Compact '⟳ N old-engine' chip counting SAVED TEXTURINGS (cache
    entries) on an older engine version — a reason to rebuild. Counts
    cache entries, not PDFs, so it stays honest about its source."""
    if not _structured_engine_active():
        return ""
    from fnd.tui.upgrade_banner import count_pre_upgrade_entries

    n, _ = count_pre_upgrade_entries()
    return f"⟳ {n} old-engine" if n else ""


def _summary_rebuild_all(app: FNDApp) -> str:
    """Trailing for the 'Rebuild all collections' row. Nudges with the
    number of SAVED TEXTURINGS (cache entries) on an older engine version
    — counts cache entries, not PDFs (an orphaned entry counts too), so it
    states its source honestly rather than implying a PDF tally."""
    from fnd.tui.lazy_trailing import get_or_schedule

    def _compute() -> str:
        if not _structured_engine_active():
            return "structured extraction unavailable"
        from fnd.tui.upgrade_banner import count_pre_upgrade_entries

        n, _ = count_pre_upgrade_entries()
        return f"⟳ {n} saved on an older engine" if n else "all on current engine"

    return get_or_schedule(app, "cache.rebuild_all", _compute)


def _summary_indexing_pdf_texture(app: FNDApp) -> str:
    """Trailing summary for the combined Indexing & PDF Texture root row.

    Composes the two prior chip summaries so the root row carries the
    most actionable status bits from both subsections at a glance."""
    halves = [t for t in (_summary_indexing(app), _summary_pdf_texture(app)) if t]
    return " · ".join(halves)


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


def _summary_files_in_index(app: FNDApp) -> str:
    """Lazy walker: distinct file count in the tantivy index, plus the
    collection count from config."""
    from fnd.tui.lazy_trailing import get_or_schedule

    return get_or_schedule(app, "indexing.files_in_index", _compute_files_in_index)


def _compute_files_in_index() -> str:
    try:
        import tantivy

        from fnd.config import default_index_dir, load
        from fnd.schema import F_COLLECTION, F_PARENT_ID

        index_dir = default_index_dir()
        if not index_dir.exists():
            return "no index yet"
        index = tantivy.Index.open(str(index_dir))
        index.reload()
        searcher = index.searcher()
        cfg = load()
        col_count = len(cfg.collections)
        parents: set[str] = set()
        for name in cfg.collections:
            q = tantivy.Query.term_query(index.schema, F_COLLECTION, name)
            for _score, addr in searcher.search(q, limit=200000).hits:
                pid = searcher.doc(addr).get_first(F_PARENT_ID)  # type: ignore[attr-defined]
                if pid:
                    parents.add(str(pid))
        return f"{len(parents):,} across {col_count} collection{'s' if col_count != 1 else ''}"
    except Exception:
        return "unavailable"


def _summary_pdfs_textured(app: FNDApp) -> str:
    """Lazy walker: distinct textured PDFs in the index, vs total PDFs
    visible on disk under every collection's sources."""
    from fnd.tui.lazy_trailing import get_or_schedule

    return get_or_schedule(app, "pdf_texture.textured_count", _compute_pdfs_textured)


def _open_still_flat_drill(app: FNDApp) -> None:
    from fnd.tui.settings_screen import StillFlatDrillIn

    app.push_screen(StillFlatDrillIn())


def _compute_pdfs_textured() -> str:
    import contextlib

    try:
        from pathlib import Path

        import tantivy

        from fnd.config import default_index_dir, load
        from fnd.schema import F_BODY_MD, F_KIND, F_PATH

        cfg = load()
        # Y: every PDF the indexer would actually pick up under any
        # collection's source. Uses the same walk_sources call the
        # indexer uses so includes/excludes/frontmatter filters are
        # honoured - otherwise a vault that's md-only for some
        # collections would inflate the total with PDFs that can
        # never be indexed under any of those collections.
        from fnd.walk import walk_sources

        on_disk: set[str] = set()
        for col in cfg.collections.values():
            for path in walk_sources(sources=list(col.sources)):
                if path.suffix.lower() != ".pdf":
                    continue
                with contextlib.suppress(OSError):
                    on_disk.add(str(path.resolve()))
        total = len(on_disk)
        if total == 0:
            return "no PDFs"

        # X: PDFs in the index with at least one chunk carrying body_md —
        # the Markdown texturing payload that drives the structural
        # preview. body_struct (flat Blocks) is present on EVERY indexed
        # PDF, so it can't distinguish textured from flat; body_md is the
        # honest signal. Falls back to "no index" when the index is missing.
        index_dir = default_index_dir()
        if not index_dir.exists():
            return f"0 of {total} · ⚠ {total} still flat"
        index = tantivy.Index.open(str(index_dir))
        index.reload()
        searcher = index.searcher()
        kind_q = tantivy.Query.term_query(index.schema, F_KIND, "pdf")
        hits = searcher.search(kind_q, limit=200000).hits
        textured_paths: set[str] = set()
        for _score, addr in hits:
            doc = searcher.doc(addr)
            body = doc.get_first(F_BODY_MD)  # type: ignore[attr-defined]
            if not body:
                continue
            path = doc.get_first(F_PATH)  # type: ignore[attr-defined]
            if path:
                with contextlib.suppress(OSError):
                    textured_paths.add(str(Path(str(path)).resolve()))
        textured = len(textured_paths & on_disk)
        flat = total - textured
        flat_chip = f" · ⚠ {flat} still flat" if flat > 0 else ""
        return f"{textured:,} of {total:,}{flat_chip}"
    except Exception:
        return "unavailable"


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


def _run_prune_orphans(app: FNDApp) -> None:
    """Remove texturings for files no longer on disk. Hashing the corpus
    to find what's live is slow, so run it off the UI thread and notify
    the result. Orphans are entries for files that no longer exist, so
    removal is safe — re-indexing re-creates them only if a file returns."""
    import contextlib
    import threading

    from fnd.cache import ExtractionCache, default_cache_dir

    cfg = app._config  # type: ignore[attr-defined]
    if cfg is None or not cfg.collections:
        with contextlib.suppress(Exception):
            app.notify("No collections to scan.")
        return
    if not default_cache_dir().exists():
        with contextlib.suppress(Exception):
            app.notify("PDF Texture Cache is empty.")
        return
    with contextlib.suppress(Exception):
        app.notify("Scanning sources for orphaned texturings…")

    def _work() -> None:
        from fnd.texture_maintenance import live_content_shas

        try:
            removed = ExtractionCache().prune_orphans(live_content_shas(cfg))
            msg = (
                f"Removed {removed} orphaned texturing(s)."
                if removed
                else "No orphaned texturings — cache is clean."
            )
        except Exception as e:
            msg = f"Orphan prune failed: {e}"
        with contextlib.suppress(Exception):
            app.call_from_thread(app.notify, msg)

    threading.Thread(target=_work, daemon=True).start()


def _run_cache_clear(app: FNDApp) -> None:
    import contextlib

    from rich.text import Text

    from fnd.cache import ExtractionCache, default_cache_dir
    from fnd.tui.settings_screen import CacheMaintenanceConfirm

    root = default_cache_dir()
    cache = ExtractionCache()
    n = cache.entry_count() if root.exists() else 0
    size = cache.total_size_bytes() if root.exists() else 0
    if n == 0:
        with contextlib.suppress(Exception):
            app.notify("PDF Texture Cache is empty.")
        return

    from fnd.tui.cost_estimate import estimate_seconds_for, format_duration

    eta_s = estimate_seconds_for(n) if n else 0.0
    summary = Text()
    summary.append("Saved texturings: ", style="dim")
    summary.append(f"{n}\n", style="bold")
    summary.append("Size:             ", style="dim")
    summary.append(f"{_human_bytes(size)}\n", style="bold")
    summary.append("Path:             ", style="dim")
    summary.append(f"{root}\n\n", style="bold")
    summary.append(
        "Frees this disk space. Previews you've already built keep working "
        "(texturing lives in the index, not the cache). A later Rebuild "
        f"re-creates entries as needed — re-texturing cost then ~{format_duration(eta_s)}.",
        style="dim",
    )

    def _do_clear() -> int:
        import shutil

        shutil.rmtree(root, ignore_errors=True)
        return n

    app.push_screen(
        CacheMaintenanceConfirm(
            title="Indexing › PDF Texture Cache › Clear texture cache",
            summary=summary,
            run=_do_clear,
            confirm_label="Yes, clear the texture cache",
            result_label="saved texturings removed",
            irreversible=True,
        )
    )


def _provider_filters(app: FNDApp) -> tuple[MenuItem, ...]:
    """Filters, split by when they apply.

    Index-time filters decide what the index holds; query-time ones narrow
    what a search returns from it. On one screen the difference is visible;
    apart, the two read identically.
    """
    return (
        header("What gets indexed", level=2),
        *_provider_index_filters(app),
        MenuItem(
            id="filters.tag_frontmatter_keys",
            label="Extra frontmatter tag keys",
            description=(
                "Frontmatter fields to treat as tags beyond tags:, "
                "comma-separated — e.g. Course, Notes_Type, Topic. Values are "
                "grouped under the key in the Tags pane (course/algebra), so "
                "they never collide with a plain tag. Matched "
                "case-insensitively. Needs a reindex to take effect."
            ),
            kind=KIND_SCALAR,
            setting_path="defaults.tag_frontmatter_keys",
            hint="Course, Notes_Type, Topic",
            coerce=_coerce_str_list,
            value_getter=_get_str_list_default("tag_frontmatter_keys"),
            keywords=("tag", "tags", "frontmatter", "key", "course", "custom"),
        ),
        header("What a search returns", level=2),
        MenuItem(
            id="filters.tag_sources",
            label="Tag sources",
            description=(
                "Which sources feed the Tags filter, comma-separated. "
                "'frontmatter' reads a note's YAML tags:; 'os' reads macOS "
                "Finder tags"
                + ("" if os_labels.is_macos() else " (macOS only — inert here)")
                + ". Leave empty to turn tag filtering off. "
                "Toggling a source takes effect immediately — no reindex."
            ),
            kind=KIND_SCALAR,
            setting_path="defaults.tag_sources",
            hint="frontmatter, os",
            coerce=_coerce_str_list,
            value_getter=_get_str_list_default("tag_sources"),
            keywords=("tag", "tags", "frontmatter", "finder", "source"),
        ),
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
            id=f"root.{SECTION_FILTERS}",
            label="Filters",
            description=(
                "What enters the index, and what a search returns from it — "
                "file types, tags, size, dates and ignore files."
            ),
            kind=KIND_EXTERNAL,
            external=_open_section(SECTION_FILTERS),
            value_getter=_summary_index_filters,
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
            id=f"root.{SECTION_INDEXING_PDF_TEXTURE}",
            label="Indexing & PDF Texture",
            description=(
                "Run Update across all collections, manage the texturising "
                "engine and PDF Texture Cache, and tune auto-resume + "
                "while-indexing behaviour. Per-collection updates live "
                "under each collection."
            ),
            kind=KIND_EXTERNAL,
            external=_open_section(SECTION_INDEXING_PDF_TEXTURE),
            value_getter=_summary_indexing_pdf_texture,
            keywords=(
                "index",
                "indexer",
                "reindex",
                "process",
                "new",
                "auto-resume",
                "pdf",
                "texture",
                "texturise",
                "textured",
                "flat",
                "engine",
                "cache",
                # Legacy.
                "structured",
                "structure",
                "pdf-structure",
                "extraction",
            ),
        ),
        header("External", level=2, anchor_id="external"),
        MenuItem(
            id="root.open_config_file",
            label="Config file",
            description=(
                "Open config.toml in $EDITOR; reload on save. Shift+⏎ reveals in "
                f"{os_labels.file_manager_phrase()}."
            ),
            kind=KIND_EXTERNAL,
            external=_open_config_file_action,
            value_getter=_summary_config_path,
            external_app=True,
            keywords=("edit", "config", "toml", "open", "external"),
        ),
        MenuItem(
            id="root.open_keybindings_file",
            label="Keybindings file",
            description=(
                "Open keybindings.toml in $EDITOR. Shift+⏎ reveals in "
                f"{os_labels.file_manager_phrase()}."
            ),
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
    SECTION_INDEXING_PDF_TEXTURE: _provider_indexing_pdf_texture,
    SECTION_FILTERS: _provider_filters,
}

_SECTION_LABELS: dict[str, str] = {
    SECTION_PREFERENCES: "Preferences",
    SECTION_COLLECTIONS: "Collections",
    SECTION_KEYBINDINGS: "Keybindings",
    SECTION_INDEXING_PDF_TEXTURE: "Indexing & PDF Texture",
    SECTION_FILTERS: "Filters",
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


def _pseudo_copy_command_row() -> MenuItem:
    """A command row that copies the current search as a runnable `fnd`
    command. Lives here (not a config setting) so it's reachable from the
    palette; the same action is bound to a key in the Keybindings list."""
    return MenuItem(
        id="pseudo.copy_query_command",
        label="Copy query as command",
        description=(
            "Copy the current query and active filters to the clipboard as a "
            "runnable `fnd` command, so the search can be saved and re-run."
        ),
        kind=KIND_ACTION,
        action_id="copy_query_command",
        action_label="Copy",
        keywords=("copy", "command", "clipboard", "cli", "share", "export", "query"),
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
    yield (), _pseudo_copy_command_row()
