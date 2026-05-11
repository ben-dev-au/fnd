"""Unified Settings & Commands menu — data model.

The menu is a tree of :class:`MenuItem` records, one row per item the user
sees in the Settings screen. ``kind`` decides what ``Enter`` does:

  ``header``    Non-selectable group separator row. Two visual levels:
                ``header_level=1`` (top-level, drawn with rule lines) and
                ``header_level=2`` (sub-group, bold-only).
  ``submenu``   Push a new :class:`SettingsScreen` with this item's children.
  ``action``    Run ``AcornApp.action_<action_id>()`` (REGISTRY action) and
                close the settings stack.
  ``scalar``    Open the bottom edit-bar for a single config field. The
                value is written through :func:`acorn.config.write_setting`
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

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from acorn.tui.app import AcornApp


# ── Kinds ────────────────────────────────────────────────────────────

KIND_HEADER = "header"
KIND_SUBMENU = "submenu"
KIND_ACTION = "action"
KIND_SCALAR = "scalar"
KIND_TOGGLE = "toggle"
KIND_PICKER = "picker"
KIND_EXTERNAL = "external"

# Section ids the `?` / F3 shortcuts push directly as sub-screens.
SECTION_KEYBINDINGS = "keybindings"
SECTION_PREFERENCES = "preferences"
SECTION_COLLECTIONS = "collections"


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
    provider: Callable[[AcornApp], tuple[MenuItem, ...]] | None = None

    # ACTION
    action_id: str = ""

    # SCALAR
    setting_path: str = ""
    hint: str = ""
    coerce: Callable[[str], Any] | None = None
    value_getter: Callable[[AcornApp], str] | None = None

    # TOGGLE
    toggle_getter: Callable[[AcornApp], bool] | None = None
    toggle_setter: Callable[[AcornApp, bool], None] | None = None

    # PICKER
    multi: bool = False
    choices_provider: Callable[[AcornApp], list[ChoiceOption]] | None = None
    picker_getter: Callable[[AcornApp], Any] | None = None
    picker_setter: Callable[[AcornApp, Any], None] | None = None

    # EXTERNAL
    external: Callable[[AcornApp], None] | None = None

    # Metadata used by the cross-tree search view.
    keywords: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_header(self) -> bool:
        return self.kind == KIND_HEADER

    @property
    def is_selectable(self) -> bool:
        return self.kind != KIND_HEADER

    def trailing_value(self, app: AcornApp) -> str:
        """Right-aligned trailing column. Setting kinds carry the live
        value; drill kinds and actions render empty trailing slots (we
        dropped the ``▶`` chevron — drilling is implicit)."""
        try:
            if self.kind == KIND_SCALAR and self.value_getter is not None:
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

    def resolve_children(self, app: AcornApp) -> tuple[MenuItem, ...]:
        if self.provider is not None:
            try:
                return tuple(self.provider(app))
            except Exception:
                return ()
        return self.children


# ── Header helpers ───────────────────────────────────────────────────


def header(label: str, *, level: int = 1, anchor_id: str = "") -> MenuItem:
    """Build a non-selectable header row."""
    return MenuItem(
        id=f"header.{anchor_id or label.lower().replace(' ', '_')}",
        label=label,
        kind=KIND_HEADER,
        header_level=level,
    )


# ── Tree walking ────────────────────────────────────────────────────


def walk_leaves(
    item: MenuItem,
    app: AcornApp,
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


_KEYS_GLOBAL: tuple[tuple[str, str, str], ...] = (
    ("/", "focus_query", "Focus the search bar"),
    (":", "open_command_palette", "Open settings & commands"),
    ("?", "show_help", "Open keybindings"),
    ("Tab", "toggle_focus", "Toggle focus query ↔ results"),
    ("F3", "open_collections_form", "Open Settings › Collections"),
    ("Ctrl+C", "quit", "Quit"),
    ("Esc", "", "Back / cascade focus to results"),
)

_KEYS_RESULTS: tuple[tuple[str, str, str], ...] = (
    ("o", "open_at_locator", "Open at locator (Skim / default app for page)"),
    ("O", "open_default_app", "Open in default app"),
    ("Space", "peek_focused", "Quick Look"),
    ("Enter", "open_at_locator", "Open the focused match"),
    ("j / k", "", "Move cursor down / up"),
    ("← / →", "", "Collapse / expand"),
    ("h", "toggle_highlights", "Toggle search highlights in the preview"),
)

_KEYS_PREVIEW: tuple[tuple[str, str, str], ...] = (
    ("j / k", "", "Scroll line down / up"),
    ("PgDn / PgUp", "", "Scroll one page"),
)

_KEYS_FILTERS: tuple[tuple[str, str, str], ...] = (
    ("Enter", "", "Toggle filter (multi-select on kinds, radio on date)"),
    ("← / →", "", "Collapse / expand category"),
)

_KEYS_COLLECTIONS_PANEL: tuple[tuple[str, str, str], ...] = (
    ("Enter", "", "Toggle whole collection (parent) or single source"),
    ("← / →", "", "Collapse / expand"),
)

_KEYS_SETTINGS: tuple[tuple[str, str, str], ...] = (
    ("↑ / ↓", "", "Move cursor"),
    ("⏎ / →", "", "Activate / drill in"),
    ("←", "", "Back one level"),
    ("/", "", "Filter every section"),
    ("1-9", "", "Jump by index"),
    ("Esc", "", "Clear search / back one level"),
)


def _key_row(key: str, action_id: str, description: str) -> MenuItem:
    """Convert a (key, action_id, desc) tuple to a MenuItem."""
    return MenuItem(
        id=f"key.{action_id or description.lower().replace(' ', '_')}",
        label=description,
        description=description,
        kind=KIND_ACTION,
        action_id=action_id,
        key=key,
        keywords=(action_id, key) if action_id else (key,),
    )


# ── Providers ───────────────────────────────────────────────────────


def _provider_keybindings(_app: AcornApp) -> tuple[MenuItem, ...]:
    """Content of the Keybindings sub-screen. No top-level header here —
    the screen's border_title carries the identity. Sub-headers group
    by context."""
    out: list[MenuItem] = []
    for sub_label, rows in (
        ("Global", _KEYS_GLOBAL),
        ("Results pane", _KEYS_RESULTS),
        ("Preview pane", _KEYS_PREVIEW),
        ("Filters panel", _KEYS_FILTERS),
        ("Collections panel", _KEYS_COLLECTIONS_PANEL),
        ("Settings menu", _KEYS_SETTINGS),
    ):
        out.append(header(sub_label, level=2))
        out.extend(_key_row(*row) for row in rows)
    return tuple(out)


def _setting_writer(path: str) -> Callable[[AcornApp, Any], None]:
    """Setter that writes one config field and reloads the app's cached
    Config / ranking profile."""

    def _set(app: AcornApp, value: Any) -> None:
        from acorn.config import default_config_path, load, write_setting

        write_setting(
            config_path=default_config_path(),
            dotted_path=path,
            value=value,
        )
        app._config = load()  # type: ignore[attr-defined]
        app._ranking_profile = app._resolve_profile()  # type: ignore[attr-defined]
        app._refresh_status()  # type: ignore[attr-defined]

    return _set


def _get_int_default(field_name: str, fallback: int) -> Callable[[AcornApp], str]:
    def _g(app: AcornApp) -> str:
        cfg = app._config  # type: ignore[attr-defined]
        return str(getattr(cfg.defaults, field_name)) if cfg else str(fallback)

    return _g


def _get_default_collection(app: AcornApp) -> Any:
    cfg = app._config  # type: ignore[attr-defined]
    return cfg.defaults.collection if cfg else ""


def _choices_collections(app: AcornApp) -> list[ChoiceOption]:
    cfg = app._config  # type: ignore[attr-defined]
    if cfg is None:
        return []
    return [ChoiceOption(value=n, label=n) for n in sorted(cfg.collections)]


def _choices_ranking(app: AcornApp) -> list[ChoiceOption]:
    cfg = app._config  # type: ignore[attr-defined]
    if cfg is None:
        return []
    return [ChoiceOption(value=n, label=n) for n in sorted(cfg.ranking)]


def _set_highlights(app: AcornApp, value: bool) -> None:
    if app._highlights_enabled != value:  # type: ignore[attr-defined]
        app.action_toggle_highlights()


def _provider_preferences(_app: AcornApp) -> tuple[MenuItem, ...]:
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
    )


# ── Collections drill chain (per-collection / per-source) ───────────


def _collection_summary(app: AcornApp, name: str) -> str:
    """Trailing slot for a collection row in the root list — shows
    source count plus an `●` if the collection is active."""
    cfg = app._config  # type: ignore[attr-defined]
    if cfg is None or name not in cfg.collections:
        return ""
    n = len(cfg.collections[name].sources)
    active = "●" if name in (app._collections or []) else "○"  # type: ignore[attr-defined]
    return f"{active} {n} source{'s' if n != 1 else ''}"


def _make_open_collection_screen(name: str) -> Callable[[AcornApp], None]:
    """Push a sub-SettingsScreen for managing ``name``."""

    def _open(app: AcornApp) -> None:
        from acorn.tui.settings_screen import SettingsScreen

        items = _provider_collection(app, name)
        app.push_screen(
            SettingsScreen(
                breadcrumb=("Collections", name),
                items=items,
                root_provider=_provider_root,
            )
        )

    return _open


def _make_open_sources_screen(name: str) -> Callable[[AcornApp], None]:
    def _open(app: AcornApp) -> None:
        from acorn.tui.settings_screen import SettingsScreen

        items = _provider_sources(app, name)
        app.push_screen(
            SettingsScreen(
                breadcrumb=("Collections", name, "Sources"),
                items=items,
                root_provider=_provider_root,
            )
        )

    return _open


def _make_open_source_form(name: str, index: int | None) -> Callable[[AcornApp], None]:
    """index=None means 'add new source'."""

    def _open(app: AcornApp) -> None:
        from acorn.tui.settings_screen import SourceFormScreen

        app.push_screen(SourceFormScreen(collection_name=name, source_index=index))

    return _open


def _make_open_rename(name: str) -> Callable[[AcornApp], None]:
    def _open(app: AcornApp) -> None:
        from acorn.tui.settings_screen import RenameCollectionScreen

        app.push_screen(RenameCollectionScreen(collection_name=name))

    return _open


def _make_open_delete_confirm(name: str) -> Callable[[AcornApp], None]:
    def _open(app: AcornApp) -> None:
        from acorn.tui.settings_screen import DeleteCollectionScreen

        app.push_screen(DeleteCollectionScreen(collection_name=name))

    return _open


def _make_reindex(name: str) -> Callable[[AcornApp], None]:
    def _run(app: AcornApp) -> None:
        # Use the existing background-worker entry point on the app.
        app._reindex_collection_async(name)  # type: ignore[attr-defined]

    return _run


def _make_add_collection() -> Callable[[AcornApp], None]:
    def _open(app: AcornApp) -> None:
        from acorn.tui.settings_screen import NewCollectionScreen

        app.push_screen(NewCollectionScreen())

    return _open


def _open_config_file_action(app: AcornApp) -> None:
    app.action_open_config_file()


def _provider_collections(app: AcornApp) -> tuple[MenuItem, ...]:
    """Content of the Collections sub-screen — `Add collection` action
    followed by one drill-in row per configured collection."""
    cfg = app._config  # type: ignore[attr-defined]
    names = sorted(cfg.collections.keys()) if cfg else []
    items: list[MenuItem] = [
        MenuItem(
            id="collections.add",
            label="Add collection",
            kind=KIND_EXTERNAL,
            external=_make_add_collection(),
            keywords=("add", "new"),
        ),
    ]
    for name in names:
        items.append(
            MenuItem(
                id=f"collection.{name}",
                label=name,
                description=f"Edit sources, ranking, delete {name}.",
                kind=KIND_EXTERNAL,
                external=_make_open_collection_screen(name),
                value_getter=(lambda n: lambda app: _collection_summary(app, n))(name),
                keywords=(name,),
            )
        )
    return tuple(items)


def _provider_collection(app: AcornApp, name: str) -> tuple[MenuItem, ...]:
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
                lambda n: lambda app: (
                    f"{len(app._config.collections[n].sources)} source(s)"  # type: ignore[attr-defined]
                    if app._config and n in app._config.collections  # type: ignore[attr-defined]
                    else ""
                )
            )(name),
        ),
        MenuItem(
            id=f"col.{name}.ranking_profile",
            label="Ranking profile",
            kind=KIND_PICKER,
            choices_provider=_choices_ranking,
            picker_getter=(
                lambda n: lambda app: (
                    app._config.collections[n].ranking_profile  # type: ignore[attr-defined]
                    if app._config and n in app._config.collections  # type: ignore[attr-defined]
                    else ""
                )
            )(name),
            picker_setter=(
                lambda n: lambda app, value: _set_collection_ranking_profile(app, n, value)
            )(name),
        ),
        MenuItem(
            id=f"col.{name}.reindex",
            label="Reindex",
            description="Drop existing chunks and rebuild from the current source set.",
            kind=KIND_EXTERNAL,
            external=_make_reindex(name),
        ),
        MenuItem(
            id=f"col.{name}.delete",
            label="Delete collection…",
            description="Removes the collection from config and drops its chunks.",
            kind=KIND_EXTERNAL,
            external=_make_open_delete_confirm(name),
        ),
    )


def _set_collection_ranking_profile(app: AcornApp, name: str, value: Any) -> None:
    """Picker setter for a per-collection ranking_profile field."""
    from acorn.config import default_config_path, load, write_setting

    write_setting(
        config_path=default_config_path(),
        dotted_path=f"collections.{name}.ranking_profile",
        value=value,
    )
    app._config = load()  # type: ignore[attr-defined]
    app._ranking_profile = app._resolve_profile()  # type: ignore[attr-defined]
    app._refresh_status()  # type: ignore[attr-defined]


def _provider_sources(app: AcornApp, name: str) -> tuple[MenuItem, ...]:
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
    ]
    col = cfg.collections[name]
    for i, src in enumerate(col.sources):
        from pathlib import Path

        path_display = str(src.path) if src.path else "(no path)"
        items.append(
            MenuItem(
                id=f"sources.{name}.{i}",
                label=f"{i + 1}. {Path(path_display).name or path_display}",
                description=path_display,
                kind=KIND_EXTERNAL,
                external=_make_open_source_form(name, i),
                value_getter=lambda _app, p=path_display: p,
            )
        )
    return tuple(items)


# ── Configuration ──────────────────────────────────────────────────


# ── Root menu (small list of drill-in categories) ───────────────────


def _open_section(section_id: str) -> Callable[[AcornApp], None]:
    """Build an external callable that pushes the named sub-screen."""

    def _open(app: AcornApp) -> None:
        from acorn.tui.settings_screen import open_settings_section

        open_settings_section(app, section_id)

    return _open


def _provider_root(_app: AcornApp) -> tuple[MenuItem, ...]:
    """Root settings menu — a clean, short list of categories. No
    content piled on top of each other. Each drill-in row pushes its
    own sub-screen."""
    return (
        MenuItem(
            id=f"root.{SECTION_PREFERENCES}",
            label="Preferences",
            description="Result limits, debounce, defaults, display options.",
            kind=KIND_EXTERNAL,
            external=_open_section(SECTION_PREFERENCES),
        ),
        MenuItem(
            id=f"root.{SECTION_COLLECTIONS}",
            label="Collections",
            description="Add, edit, or delete collections and their sources.",
            kind=KIND_EXTERNAL,
            external=_open_section(SECTION_COLLECTIONS),
        ),
        MenuItem(
            id=f"root.{SECTION_KEYBINDINGS}",
            label="Keybindings",
            description="Every key and what it does. Searchable.",
            kind=KIND_EXTERNAL,
            external=_open_section(SECTION_KEYBINDINGS),
        ),
        MenuItem(
            id="root.open_config_file",
            label="Open config file in editor",
            description="Drop into $EDITOR on config.toml; reload on save.",
            kind=KIND_EXTERNAL,
            external=_open_config_file_action,
            keywords=("edit", "config", "toml"),
        ),
    )


def build_root_items(app: AcornApp) -> tuple[MenuItem, ...]:
    """The rows the root :class:`SettingsScreen` renders."""
    return _provider_root(app)


# Section providers exposed by id — used by `open_settings_section` and
# the `?` / F3 shortcuts to push a specific sub-screen directly.
_SECTION_PROVIDERS: dict[str, Callable[[AcornApp], tuple[MenuItem, ...]]] = {
    SECTION_PREFERENCES: _provider_preferences,
    SECTION_COLLECTIONS: _provider_collections,
    SECTION_KEYBINDINGS: _provider_keybindings,
}

_SECTION_LABELS: dict[str, str] = {
    SECTION_PREFERENCES: "Preferences",
    SECTION_COLLECTIONS: "Collections",
    SECTION_KEYBINDINGS: "Keybindings",
}


def section_items(app: AcornApp, section_id: str) -> tuple[MenuItem, ...]:
    """Return the rows for a named sub-screen."""
    provider = _SECTION_PROVIDERS.get(section_id)
    return provider(app) if provider else ()


def section_label(section_id: str) -> str:
    return _SECTION_LABELS.get(section_id, section_id)
