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
from pathlib import Path
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
        value; drill rows obey ``drill_summary_mode`` from config."""
        try:
            cfg = getattr(app, "_config", None)
            if cfg is None:
                from acorn.config import load as _load_cfg

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
    )


# ── Collections drill chain (per-collection / per-source) ───────────


def _collection_summary(app: AcornApp, name: str) -> str:
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
        from acorn.tui.settings_screen import AddCollectionWizard

        app.push_screen(AddCollectionWizard())

    return _open


def _open_config_file_action(app: AcornApp) -> None:
    app.action_open_config_file()


def _open_keybindings_file_action(app: AcornApp) -> None:
    app.action_open_keybindings_file()  # type: ignore[attr-defined]


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


def _source_trailing(collection_name: str, idx: int) -> Callable[[AcornApp], str]:
    """Build a value_getter for a per-source row that shows file-types
    and a path-not-found warning when the source directory is missing."""

    def _summary(app: AcornApp) -> str:
        from acorn.config import INDEXER_FILETYPES

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


def _open_section(section_id: str) -> Callable[[AcornApp], None]:
    """Build an external callable that pushes the named sub-screen."""

    def _open(app: AcornApp) -> None:
        from acorn.tui.settings_screen import open_settings_section

        open_settings_section(app, section_id)

    return _open


def _summary_preferences(_app: AcornApp) -> str:
    return "Result limit · Debounce · Highlights · Defaults"


def _summary_collections(app: AcornApp) -> str:
    cfg = app._config  # type: ignore[attr-defined]
    if cfg is None:
        from acorn.config import load as _load_config

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


def _summary_keybindings(app: AcornApp) -> str:
    keymap = app._acorn_keymap  # type: ignore[attr-defined]
    n_keys = len(keymap.bindings)
    return f"{n_keys} keys across 6 contexts"


def _summary_config_path(_app: AcornApp) -> str:
    from acorn.config import default_config_path

    p = str(default_config_path())
    return ("…" + p[-50:]) if len(p) > 50 else p


def _summary_keybindings_path(_app: AcornApp) -> str:
    from acorn.config import default_config_path

    p = str(default_config_path().parent / "keybindings.toml")
    return ("…" + p[-50:]) if len(p) > 50 else p


def _provider_root(_app: AcornApp) -> tuple[MenuItem, ...]:
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
            id="root.open_config_file",
            label="Open config file in editor",
            description="Drop into $EDITOR on config.toml; reload on save. Shift+Enter reveals in Finder.",
            kind=KIND_EXTERNAL,
            external=_open_config_file_action,
            value_getter=_summary_config_path,
            keywords=("edit", "config", "toml"),
        ),
        MenuItem(
            id="root.open_keybindings_file",
            label="Open keybindings file in editor",
            description="Drop into $EDITOR on keybindings.toml; Shift+Enter reveals in Finder.",
            kind=KIND_EXTERNAL,
            external=_open_keybindings_file_action,
            value_getter=_summary_keybindings_path,
            keywords=("edit", "keybindings", "rebind"),
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


def walk_all_sections(app: AcornApp) -> Iterator[tuple[tuple[str, ...], MenuItem]]:
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
