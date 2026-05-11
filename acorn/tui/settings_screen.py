"""Settings & Commands menu — rendering and dispatch (Phase 2).

The menu's *data* lives in :mod:`acorn.tui.menu`. This module renders it
as a stack of Textual ``Screen``s that share the main app's visual
vocabulary:

  * A single bordered ``Vertical#settings_box`` with the breadcrumb in
    its ``border_title`` (mirrors how the main app names panes via
    ``Tree.border_title``).
  * A plain one-line search ``Input`` at the top of the container, no
    decorative border.
  * The row list itself — a ``Vertical`` of one ``MenuRow`` widget per
    item so we can columnar-render key/label/value with Rich, skip
    headers from cursor nav, and pin trailing setting values to a fixed
    column.
  * A bottom edit-bar (mounted but hidden) that opens above the shared
    hint bar when editing a scalar — same chrome wherever it appears.
  * A shared ``#footer_hints`` Static docked at the screen bottom,
    rendered by :func:`acorn.tui.app.render_hint_bar` so the visual is
    identical to the main app's footer.

Drilling into a collection / source / picker pushes another
``Screen`` onto Textual's ``screen_stack``. ``Esc`` pops one level
naturally; no pre-popping or manual back stacks.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from rich.text import Text
from textual import events, on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.reactive import reactive
from textual.screen import Screen
from textual.widget import Widget
from textual.widgets import Input, OptionList, Static, TextArea
from textual.widgets.option_list import Option

from acorn.tui.menu import (
    KIND_ACTION,
    KIND_EXTERNAL,
    KIND_HEADER,
    KIND_PICKER,
    KIND_SCALAR,
    KIND_SUBMENU,
    KIND_TOGGLE,
    ChoiceOption,
    MenuItem,
    build_root_items,
    section_items,
    section_label,
    walk_all_sections,
)
from acorn.tui.widgets import DetailStrip

if TYPE_CHECKING:
    from acorn.tui.app import AcornApp


# Width budget for key column in Keys & Actions rows. Anything wider gets
# truncated rather than pushing the description.
_KEY_COL = 12


def _hint_bar(app: AcornApp, contextual: tuple[tuple[str, str], ...]) -> Any:
    """Build the shared hint-bar Text for a Settings screen. Anchors
    come from the main app (single source of truth)."""
    from acorn.tui.app import render_hint_bar

    return render_hint_bar(app._FOOTER_ANCHORS, contextual)  # type: ignore[attr-defined]


_SETTINGS_HINTS: tuple[tuple[str, str], ...] = (
    ("↑↓", "Nav"),
    ("⏎", "Open"),
    ("←", "Back"),
    ("/", "Filter"),
    ("Esc", "Back"),
)


# ── Row rendering ────────────────────────────────────────────────────


def _render_row(
    item: MenuItem,
    app: AcornApp | None,
    width: int | None = None,
    breadcrumb: tuple[str, ...] | None = None,
) -> Text:
    """Render one menu row as Rich Text.

    Layout (left to right):
      [key]  label  ……………… trailing_value
                               (or breadcrumb in dim italic when filtering)

    - Keys (Keybindings rows) render as ``[<key>]`` in $accent bold,
      bracketed in $text-muted for a subtle frame.
    - Labels render in $text.
    - Trailing values right-align in $primary bold (setting values) or
      $text-muted italic (drill row summaries / search breadcrumbs).

    ``app`` may be ``None`` for tests that don't construct a full app —
    in that case the trailing-value lookup is skipped.

    ``breadcrumb`` is a tuple of section labels — when provided it is
    rendered instead of the normal trailing value so the user knows
    which section each cross-section search result comes from.
    """
    if item.kind == KIND_HEADER:
        return _render_header(item, width)

    text = Text()
    if item.key:
        # Bracketed key in 12-char column: "[<key>]" + padding.
        # Rich's `Text` style strings don't resolve Textual theme vars
        # ($accent etc.), so we use plain `bold` for the key glyph and
        # let the surrounding CSS apply the accent colour.
        bracket_open = Text("[", style="dim")
        key_glyph = Text(item.key, style="bold")
        bracket_close = Text("]", style="dim")
        key_field = bracket_open + key_glyph + bracket_close
        # Pad to 12 columns so labels align across rows.
        used = len(item.key) + 2  # brackets + key
        key_field.append(" " * max(1, _KEY_COL - used))
        text.append_text(key_field)
    text.append(item.label)
    if breadcrumb:
        # Cross-section search: show the section path instead of trailing value.
        bc_text = " › ".join(breadcrumb)
        if width is not None:
            used = (_KEY_COL if item.key else 0) + len(item.label)
            pad = max(2, width - used - len(bc_text) - 2)
            text.append(" " + "·" * pad + " ", style="dim")
        else:
            text.append("   ")
        text.append(bc_text, style="dim italic")
    else:
        trailing = item.trailing_value(app) if app is not None else ""
        if trailing and width is not None:
            # Right-align trailing value with dotted-pad. width is the
            # available column count.
            used = (_KEY_COL if item.key else 0) + len(item.label)
            pad = max(2, width - used - len(trailing) - 2)
            text.append(" " + "·" * pad + " ", style="dim")
            text.append(trailing, style="bold")
        elif trailing:
            text.append("   ")
            text.append(trailing, style="bold")
    return text


def _render_header(item: MenuItem, _width: int | None) -> Text:
    """Group sub-header (bold + accent). Single visual style used
    everywhere a group divider is needed — no full-width rule lines
    (those were a Phase 2 misstep that made the panel feel cluttered)."""
    text = Text()
    text.append(item.label, style="bold")
    return text


# ── Bottom edit bar ──────────────────────────────────────────────────


class EditBar(Horizontal):
    """One-line scalar editor that mounts above the hint bar.

    Public state via ``open(item)`` / ``close()``. Posts an
    :class:`EditCommitted` message when the user submits a valid value;
    the parent screen updates the row display and closes the bar.
    """

    DEFAULT_CSS = """
    EditBar { dock: bottom; height: 2; padding: 0 1; background: $surface; }
    EditBar.-hidden { display: none; }
    EditBar > Static.-edit-label { color: $text-muted; width: auto; }
    EditBar > Input#editor_input { border: none; padding: 0 1; color: $primary; background: $surface; width: 1fr; }
    EditBar > Static.-edit-error { color: $error; width: auto; }
    EditBar > Static.-edit-error.-ok { color: $success; }
    EditBar > Static.-edit-error.-warn { color: $warning; }
    """

    class EditCommitted(Message):
        """Posted on a successful save."""

        def __init__(self, item: MenuItem, value: Any) -> None:
            super().__init__()
            self.item = item
            self.value = value

    def __init__(self) -> None:
        super().__init__()
        self.add_class("-hidden")
        self._item: MenuItem | None = None

    def compose(self) -> ComposeResult:
        yield Static("", classes="-edit-label")
        yield Input(id="editor_input", placeholder="")
        yield Static("", classes="-edit-error")

    def open(self, item: MenuItem, current_value: str) -> None:
        self._item = item
        label_widget = self.query_one(Static)
        self.query_one(".-edit-error", Static).update("")
        hint_suffix = f" · {item.hint}" if item.hint else ""
        label_widget.update(Text(f"Edit {item.label}{hint_suffix} ", style="dim"))
        editor = self.query_one("#editor_input", Input)
        editor.value = current_value
        self.remove_class("-hidden")
        editor.focus()

    def close(self) -> None:
        self._item = None
        self.add_class("-hidden")
        # Push focus back to the list so the user can keep navigating.
        import contextlib

        with contextlib.suppress(Exception):
            self.screen.query_one(SettingsList).focus()

    def show_error(self, message: str) -> None:
        label = self.query_one(".-edit-error", Static)
        label.remove_class("-ok")
        label.remove_class("-warn")
        label.update(message)

    def _set_status(self, text: str, *, tone: str = "error") -> None:
        """Update the inline status label with one of the styling tones.

        tone ∈ {"error" (default red, set by base class), "ok" (success),
        "warn" (warning)}.
        """
        label = self.query_one(".-edit-error", Static)
        label.remove_class("-ok")
        label.remove_class("-warn")
        if tone == "ok":
            label.add_class("-ok")
        elif tone == "warn":
            label.add_class("-warn")
        label.update(text)

    @on(Input.Changed, "#editor_input")
    def _on_input_changed(self, ev: Input.Changed) -> None:
        """For the wizard's Source path row, validate on every keystroke
        and surface ✓/✗ inline in the (repurposed) error label."""
        if self._item is None:
            return
        if self._item.id != "wiz.path":
            return
        from pathlib import Path as _Path

        raw = ev.value.strip().strip("'\"")
        if not raw:
            self._set_status("", tone="error")
            return
        p = _Path(raw).expanduser()
        if not p.exists():
            self._set_status("✗ does not exist", tone="error")
            return
        if not p.is_dir():
            self._set_status("⚠ not a directory", tone="warn")
            return
        try:
            n = sum(1 for _ in p.iterdir())
        except OSError:
            self._set_status("⚠ unreadable", tone="warn")
            return
        self._set_status(f"✓ {n} entries", tone="ok")

    @on(Input.Submitted, "#editor_input")
    def _on_submit(self, ev: Input.Submitted) -> None:
        if self._item is None:
            return
        raw = ev.value.strip()
        coerce = self._item.coerce or str
        try:
            value: Any = coerce(raw) if raw else raw
        except (TypeError, ValueError) as e:
            self.show_error(f"invalid: {e}")
            return
        self.post_message(self.EditCommitted(self._item, value))

    def on_key(self, ev: events.Key) -> None:
        if ev.key == "escape":
            ev.stop()
            self.close()


# ── List body ────────────────────────────────────────────────────────


class SettingsList(Widget, can_focus=True):
    """The scrollable list of MenuRow widgets.

    Built as a custom focusable container (rather than OptionList) so we
    can:
      * Render columnar headers / rows via Rich without OptionList's
        single-string Prompt model.
      * Skip cursor over header rows directionally (Up moves to previous
        non-header, Down to next non-header).
      * Reuse the parent-skip behaviour the rest of the app uses for
        tree parents.
    """

    DEFAULT_CSS = """
    SettingsList { height: 1fr; }
    SettingsList > Vertical { padding: 0 0; }
    SettingsList Static.row { height: 1; padding: 0 1; }
    SettingsList Static.row.-header-1 { padding: 1 0 0 0; height: 2; }
    SettingsList Static.row.-header-2 { padding: 0 0 0 0; }
    SettingsList Static.row.-cursor { background: $accent 40%; text-style: bold; }
    """

    BINDINGS = [  # noqa: RUF012
        Binding("up,k", "move(-1)", show=False),
        Binding("down,j", "move(1)", show=False),
        Binding("home", "move_home", show=False),
        Binding("end", "move_end", show=False),
        Binding("page_up", "page(-1)", show=False),
        Binding("page_down", "page(1)", show=False),
        # Enter = full activation (drill / save / toggle / run). Right is
        # navigation parity only: drills sub-screens, no-op on
        # scalars / toggles / actions / leaf rows.
        Binding("enter", "activate", show=False),
        Binding("right", "drill", show=False),
        # Shift+Enter = reveal in Finder (file-capable rows only).
        Binding("shift+enter", "reveal", show=False),
        # Numeric jumps stay as a hidden affordance.
        *(Binding(str(n), f"jump({n})", show=False) for n in range(1, 10)),
    ]

    cursor_index: reactive[int] = reactive(0)

    class Activated(Message):
        def __init__(self, item: MenuItem) -> None:
            super().__init__()
            self.item = item

    class Highlighted(Message):
        def __init__(self, item: MenuItem | None) -> None:
            super().__init__()
            self.item = item

    def __init__(self) -> None:
        super().__init__()
        self._items: list[MenuItem] = []
        # Maps id(item) → breadcrumb tuple during cross-section search.
        # Empty when no filter is active.
        self._search_breadcrumbs: dict[int, tuple[str, ...]] = {}

    def compose(self) -> ComposeResult:
        yield Vertical(id="settings_list_body")

    # ── Population ──────────────────────────────────────────────

    def set_items(
        self,
        items: list[MenuItem],
        breadcrumbs: dict[int, tuple[str, ...]] | None = None,
    ) -> None:
        self._items = list(items)
        self._search_breadcrumbs = dict(breadcrumbs) if breadcrumbs else {}
        body = self.query_one("#settings_list_body", Vertical)
        # Remove existing rows synchronously by walking the DOM directly —
        # Textual's `remove_children` is async and would race against the
        # mount of fresh rows immediately below.
        for child in list(body.children):
            child.remove()
        for item in items:
            cls = "row"
            if item.kind == KIND_HEADER:
                cls += f" -header-{item.header_level or 1}"
            body.mount(Static("", classes=cls))
        self.call_after_refresh(self._init_cursor)

    def _init_cursor(self) -> None:
        first = self._first_selectable(0, +1)
        self.cursor_index = first if first is not None else 0
        self._render_all()
        self._post_highlight()

    # ── Render ──────────────────────────────────────────────────

    def _render_all(self) -> None:
        app: AcornApp = self.app  # type: ignore[assignment]
        try:
            body = self.query_one("#settings_list_body", Vertical)
        except Exception:
            return
        width = self.size.width or 80
        rows = list(body.query(Static))
        for i, (item, row) in enumerate(zip(self._items, rows, strict=False)):
            bc = self._search_breadcrumbs.get(id(item)) or None
            row.update(_render_row(item, app, width=width - 2, breadcrumb=bc))
            if i == self.cursor_index and item.kind != KIND_HEADER:
                row.add_class("-cursor")
            else:
                row.remove_class("-cursor")

    def on_resize(self, _ev: events.Resize) -> None:
        self._render_all()

    def refresh_values(self) -> None:
        """Re-render rows (after a setting change updates trailing values)."""
        self._render_all()

    # ── Cursor navigation ───────────────────────────────────────

    def _first_selectable(self, start: int, direction: int) -> int | None:
        n = len(self._items)
        i = max(0, min(n - 1, start))
        while 0 <= i < n:
            if self._items[i].kind != KIND_HEADER:
                return i
            i += direction
        return None

    def watch_cursor_index(self, _old: int, _new: int) -> None:
        self._render_all()

    def _post_highlight(self) -> None:
        if 0 <= self.cursor_index < len(self._items):
            self.post_message(self.Highlighted(self._items[self.cursor_index]))
        else:
            self.post_message(self.Highlighted(None))

    def action_move(self, delta: int) -> None:
        n = len(self._items)
        if n == 0:
            return
        i = self.cursor_index + delta
        # Skip headers.
        while 0 <= i < n and self._items[i].kind == KIND_HEADER:
            i += delta
        if not (0 <= i < n):
            return  # boundary; don't wrap
        self.cursor_index = i
        self._scroll_cursor_into_view()
        self._post_highlight()

    def action_move_home(self) -> None:
        first = self._first_selectable(0, +1)
        if first is not None:
            self.cursor_index = first
            self._scroll_cursor_into_view()
            self._post_highlight()

    def action_move_end(self) -> None:
        last = self._first_selectable(len(self._items) - 1, -1)
        if last is not None:
            self.cursor_index = last
            self._scroll_cursor_into_view()
            self._post_highlight()

    def action_page(self, direction: int) -> None:
        page = max(1, (self.size.height or 10) - 2)
        n = len(self._items)
        i = max(0, min(n - 1, self.cursor_index + page * direction))
        target = self._first_selectable(i, direction or 1)
        if target is not None:
            self.cursor_index = target
            self._scroll_cursor_into_view()
            self._post_highlight()

    def _scroll_cursor_into_view(self) -> None:
        try:
            body = self.query_one("#settings_list_body", Vertical)
            rows = list(body.query(Static))
            if 0 <= self.cursor_index < len(rows):
                self.scroll_to_widget(rows[self.cursor_index], animate=False)
        except Exception:
            pass

    # Item kinds that "drill into a sub-screen" — these are what `right`
    # activates. Right does nothing on scalars / toggles / actions / leaf
    # rows; the user must press Enter for those.
    _DRILL_KINDS: frozenset[str] = frozenset({KIND_SUBMENU, KIND_PICKER, KIND_EXTERNAL})

    def action_activate(self) -> None:
        if 0 <= self.cursor_index < len(self._items):
            item = self._items[self.cursor_index]
            if item.kind != KIND_HEADER:
                self.post_message(self.Activated(item))

    def action_drill(self) -> None:
        """Right-arrow navigation: drill into a sub-screen only. No-op
        for kinds that would otherwise *commit* state (scalar edit,
        toggle, action dispatch) — those are Enter-only."""
        if 0 <= self.cursor_index < len(self._items):
            item = self._items[self.cursor_index]
            if item.kind in self._DRILL_KINDS:
                self.post_message(self.Activated(item))

    def action_jump(self, n: int) -> None:
        """1-9 jumps to the Nth selectable item (skipping headers)."""
        target_count = n
        for i, item in enumerate(self._items):
            if item.kind == KIND_HEADER:
                continue
            target_count -= 1
            if target_count == 0:
                self.cursor_index = i
                self._scroll_cursor_into_view()
                self._post_highlight()
                self.post_message(self.Activated(item))
                return

    def action_reveal(self) -> None:
        """Shift+Enter on a reveal-capable row triggers Finder reveal of
        the underlying file. Capability is keyed off well-known row ids."""
        if not (0 <= self.cursor_index < len(self._items)):
            return
        item = self._items[self.cursor_index]
        path = self._reveal_target(item)
        if path is None:
            return
        from acorn import opener

        opener.reveal(path)

    def _reveal_target(self, item: MenuItem) -> Path | None:
        """Return the file path to reveal for ``item``, or None if the row
        isn't reveal-capable."""
        from acorn.config import default_config_path

        if item.id == "root.open_config_file":
            return default_config_path()
        if item.id == "root.open_keybindings_file":
            return Path(default_config_path()).parent / "keybindings.toml"
        return None


# ── Main list screen ────────────────────────────────────────────────


class SettingsScreen(Screen[None]):
    """One level of the Settings menu.

    Renders ``self._items`` as a :class:`SettingsList` inside a bordered
    container. Drilling into a sub-menu / external pushes another
    ``Screen`` on top; ``Esc`` pops one level naturally.
    """

    BINDINGS = [  # noqa: RUF012
        Binding("escape", "back", "Back", show=False),
        Binding("left", "back", "Back", show=False),
        Binding("slash", "focus_search", "Filter", show=False),
    ]

    CSS = """
    SettingsScreen { background: $surface; align: center middle; }
    SettingsScreen > #settings_box {
        height: auto;
        max-height: 90%;
        width: auto;
        min-width: 60;
        max-width: 100;
        border: round $primary 50%;
        padding: 0 1;
    }
    SettingsScreen > #settings_box:focus-within { border: round $accent; }
    #settings_search {
        height: 1; padding: 0 0; border: none; background: $surface; color: $text;
    }
    #settings_search:focus { color: $accent; }
    SettingsScreen > #footer_hints {
        dock: bottom; height: 1; background: $surface; padding: 0 1; color: $text-muted;
    }
    """

    def __init__(
        self,
        *,
        breadcrumb: tuple[str, ...],
        items: tuple[MenuItem, ...],
        root_provider: Any = None,
    ) -> None:
        super().__init__()
        self._breadcrumb = breadcrumb
        self._items: tuple[MenuItem, ...] = items
        self._root_provider = root_provider
        self._filter_active: bool = False
        # Populated during cross-section search: maps id(item) → breadcrumb.
        self._search_breadcrumbs: dict[int, tuple[str, ...]] = {}

    # ── Layout ──────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        title = (
            "Settings & Commands"
            if not self._breadcrumb
            else f"Settings & Commands › {' › '.join(self._breadcrumb)}"
        )
        with Vertical(id="settings_box") as box:
            box.border_title = title
            yield Input(placeholder="Type to filter…", id="settings_search")
            yield SettingsList()
            yield DetailStrip()
        yield EditBar()
        yield Static("", id="footer_hints")

    def on_mount(self) -> None:
        lst = self.query_one(SettingsList)
        lst.set_items(list(self._items))
        lst.focus()
        self._render_footer()
        self._seed_detail_strip()

    def _seed_detail_strip(self) -> None:
        """Populate the detail strip with the first selectable item so
        the user sees content immediately on open."""
        first = next((it for it in self._items if it.is_selectable), None)
        if first is not None:
            strip = self.query_one(DetailStrip)
            strip.set(first.description or "", self._row_metadata(first))

    # ── Footer ──────────────────────────────────────────────────

    def _render_footer(self) -> None:
        """Pick the hint-bar cluster based on focus, edit-bar state,
        breadcrumb, and cursor row."""
        app: AcornApp = self.app  # type: ignore[assignment]
        cluster = self._hint_cluster()
        self.query_one("#footer_hints", Static).update(_hint_bar(app, cluster))

    def _hint_cluster(self) -> tuple[tuple[str, str], ...]:
        """Choose the contextual hint cluster for the current state.

        Priority: edit-bar open > search input focused > Keybindings
        sub-screen > reveal-capable cursor row > default.
        """
        # Edit-bar open: just the save/cancel pair.
        try:
            bar = self.query_one(EditBar)
            if "-hidden" not in bar.classes:
                return (("⏎", "Save"), ("Esc", "Cancel"))
        except Exception:
            pass

        # Search input focused: hand-off / clear cluster.
        focused = self.focused
        if isinstance(focused, Input) and getattr(focused, "id", None) == "settings_search":
            return (("↓", "Results"), ("⏎", "Open first"), ("Esc", "Clear"))

        # Keybindings sub-screen: ⏎ Run · [key] Run directly · Esc Back.
        if self._breadcrumb[-1:] == ("Keybindings",):
            return (("⏎", "Run"), ("[key]", "Run directly"), ("Esc", "Back"))

        # Default cluster — possibly with Shift+⏎ Reveal appended.
        cluster: tuple[tuple[str, str], ...] = (
            ("↑↓", "Nav"),
            ("⏎", "Open"),
            ("←", "Back"),
            ("/", "Filter"),
        )
        try:
            lst = self.query_one(SettingsList)
            if 0 <= lst.cursor_index < len(lst._items):
                item = lst._items[lst.cursor_index]
                if item.id in ("root.open_config_file", "root.open_keybindings_file"):
                    cluster = (*cluster, ("Shift+⏎", "Reveal"))
        except Exception:
            pass
        return cluster

    def _refresh_hint_bar(self) -> None:
        """Public-ish entry to recompute the hint bar after focus or
        cursor-row changes."""
        import contextlib

        with contextlib.suppress(Exception):
            self._render_footer()

    # ── Search ──────────────────────────────────────────────────

    @on(Input.Changed, "#settings_search")
    def _on_search_changed(self, ev: Input.Changed) -> None:
        q = ev.value.strip().lower()
        lst = self.query_one(SettingsList)
        if not q:
            self._filter_active = False
            self._search_breadcrumbs = {}
            lst.set_items(list(self._items))
            return
        self._filter_active = True
        filtered, breadcrumbs = self._filter_items(q)
        self._search_breadcrumbs = breadcrumbs
        lst.set_items(filtered, breadcrumbs=breadcrumbs)

    def _filter_items(self, q: str) -> tuple[list[MenuItem], dict[int, tuple[str, ...]]]:
        """Cross-section: walk every section's leaves, score by substring
        match against label + key + keywords + breadcrumb segments."""
        matches: list[tuple[int, MenuItem, tuple[str, ...]]] = []
        app: AcornApp = self.app  # type: ignore[assignment]
        for path, item in walk_all_sections(app):
            if item.kind == KIND_HEADER:
                continue
            haystack = " ".join(
                (item.label, item.description, item.key, *item.keywords, *path)
            ).lower()
            idx = haystack.find(q)
            if idx == -1:
                continue
            # Earlier match in the label scores higher (smaller idx first).
            label_idx = item.label.lower().find(q)
            score = label_idx if label_idx != -1 else 1000 + idx
            matches.append((score, item, path))
        matches.sort(key=lambda m: (m[0], len(m[1].label)))
        breadcrumbs = {id(item): path for _, item, path in matches}
        return [item for _, item, _ in matches], breadcrumbs

    @on(Input.Submitted, "#settings_search")
    def _on_search_submitted(self, _ev: Input.Submitted) -> None:
        # Hand focus to the list and activate the first match.
        lst = self.query_one(SettingsList)
        lst.focus()
        lst.action_activate()

    # ── Navigation ──────────────────────────────────────────────

    def action_back(self) -> None:
        search = self.query_one("#settings_search", Input)
        if search.value:
            search.value = ""
            self._filter_active = False
            self._search_breadcrumbs = {}
            return
        import contextlib

        with contextlib.suppress(Exception):
            self.app.pop_screen()

    def action_focus_search(self) -> None:
        self.query_one("#settings_search", Input).focus()

    # ── Activation ──────────────────────────────────────────────

    @on(SettingsList.Activated)
    def _on_item_activated(self, ev: SettingsList.Activated) -> None:
        self._activate_item(ev.item)

    @on(SettingsList.Highlighted)
    def _on_item_highlighted(self, ev: SettingsList.Highlighted) -> None:
        strip = self.query_one(DetailStrip)
        item = ev.item
        if item is None:
            strip.clear()
        else:
            strip.set(item.description or "", self._row_metadata(item))
        # Hint bar may need a "Shift+⏎ Reveal" append/strip depending on row.
        self._refresh_hint_bar()

    def on_descendant_focus(self, _ev: events.DescendantFocus) -> None:
        """Re-render the hint bar when focus moves (search ↔ list)."""
        self._refresh_hint_bar()

    def on_descendant_blur(self, _ev: events.DescendantBlur) -> None:
        self._refresh_hint_bar()

    def _row_metadata(self, item: MenuItem) -> str:
        """Build the 2nd-line metadata for the detail strip — storage path,
        constraint, applicability note, etc."""
        parts: list[str] = []
        if item.setting_path:
            parts.append(f"Stored in {item.setting_path}")
        if item.hint:
            parts.append(item.hint)
        if item.action_id:
            parts.append(f"Runs {item.action_id}")
        return " · ".join(parts)

    def _activate_item(self, item: MenuItem) -> None:
        app: AcornApp = self.app  # type: ignore[assignment]
        if item.kind == KIND_ACTION:
            self._close_settings_stack()
            if item.action_id:
                method = getattr(app, f"action_{item.action_id}", None)
                if callable(method):
                    method()
            return
        if item.kind == KIND_TOGGLE:
            if item.toggle_setter is not None and item.toggle_getter is not None:
                new_value = not item.toggle_getter(app)
                try:
                    item.toggle_setter(app, new_value)
                except Exception as e:
                    self.notify(_summarize(e), severity="error")
                    return
                self.query_one(SettingsList).refresh_values()
            return
        if item.kind == KIND_SCALAR:
            current = ""
            if item.value_getter is not None:
                current = item.value_getter(app)
            self.query_one(EditBar).open(item, current)
            self._refresh_hint_bar()
            return
        if item.kind == KIND_PICKER:
            self.app.push_screen(PickerScreen(item))
            return
        if item.kind == KIND_EXTERNAL:
            if item.external is not None:
                item.external(app)
            return
        if item.kind == KIND_SUBMENU:
            children = item.resolve_children(app)
            self.app.push_screen(
                SettingsScreen(
                    breadcrumb=(*self._breadcrumb, item.label),
                    items=children,
                    root_provider=self._root_provider,
                )
            )

    @on(EditBar.EditCommitted)
    def _on_edit_committed(self, ev: EditBar.EditCommitted) -> None:
        app: AcornApp = self.app  # type: ignore[assignment]
        item = ev.item
        try:
            if item.setting_path:
                from acorn.config import default_config_path, load, write_setting

                write_setting(
                    config_path=default_config_path(),
                    dotted_path=item.setting_path,
                    value=ev.value,
                )
                app._config = load()  # type: ignore[attr-defined]
                app._ranking_profile = app._resolve_profile()  # type: ignore[attr-defined]
                app._refresh_status()  # type: ignore[attr-defined]
        except Exception as e:
            self.query_one(EditBar).show_error(_summarize(e))
            return
        self.query_one(EditBar).close()
        self.query_one(SettingsList).refresh_values()
        self.query_one(SettingsList).focus()

    def _close_settings_stack(self) -> None:
        """Pop every settings-related Screen so the user lands in the
        main app after running an action."""
        while isinstance(self.app.screen, SettingsScreen | PickerScreen):
            self.app.pop_screen()

    async def on_key(self, ev: events.Key) -> None:
        """Lazygit-style press-key-to-invoke on the Keybindings sub-screen.

        Only fires when the screen's breadcrumb ends in "Keybindings" AND
        focus is on the list (not the search input). Looks for a row whose
        ``key`` field matches the pressed key; if found, dispatches the
        action and closes the settings stack.
        """
        if self._breadcrumb[-1:] != ("Keybindings",):
            return
        focused = self.focused
        if focused is None or not isinstance(focused, SettingsList):
            return
        pressed = ev.key
        pressed_label = _normalise_key_label(pressed)
        for item in self.query_one(SettingsList)._items:
            if item.kind == KIND_HEADER or not item.key:
                continue
            # Never intercept Enter — that belongs to the regular activate path.
            if item.key.lower() == "enter":
                continue
            if item.key.lower() == pressed_label.lower():
                ev.stop()
                ev.prevent_default()
                self._close_settings_stack()
                if item.action_id:
                    method = getattr(self.app, f"action_{item.action_id}", None)
                    if callable(method):
                        method()
                return


def _normalise_key_label(key: str) -> str:
    """Map Textual's key names to the labels used in MenuItem.key."""
    return {
        "space": "Space",
        "ctrl+c": "Ctrl+C",
        "shift+enter": "Shift+Enter",
        "tab": "Tab",
        "question_mark": "?",
        "colon": ":",
        "slash": "/",
    }.get(key, key)


def _summarize(exc: Exception) -> str:
    """Single-line summary of an exception, with Pydantic-aware
    formatting for the common validation case."""
    try:
        from pydantic import ValidationError

        if isinstance(exc, ValidationError):
            errs = exc.errors()
            if errs:
                first = errs[0]
                loc = ".".join(str(p) for p in first.get("loc", ()))
                return f"{loc}: {first.get('msg', '')}"
    except ImportError:
        pass
    return str(exc)


# ── Picker (single-select / multi-select sub-screen) ────────────────


class PickerScreen(Screen[None]):
    """Sub-screen for a KIND_PICKER item.

    Single-select: Enter writes immediately and pops. Multi-select:
    Enter toggles ``✓``; Esc commits and pops.
    """

    BINDINGS = [  # noqa: RUF012
        Binding("escape,left", "back", "Back", show=False),
        Binding("enter", "activate", show=False),
        Binding("up,k", "cursor(-1)", show=False),
        Binding("down,j", "cursor(1)", show=False),
    ]

    CSS = """
    PickerScreen { background: $surface; }
    PickerScreen > #settings_box {
        height: 1fr;
        border: round $primary 50%;
        padding: 0 1;
    }
    PickerScreen > #settings_box:focus-within { border: round $accent; }
    PickerScreen > #footer_hints {
        dock: bottom; height: 1; background: $surface; padding: 0 1; color: $text-muted;
    }
    """

    def __init__(self, item: MenuItem) -> None:
        super().__init__()
        self._item = item
        self._choices: list[ChoiceOption] = []
        self._selected: set[Any] = set()

    def compose(self) -> ComposeResult:
        with Vertical(id="settings_box") as box:
            box.border_title = self._item.label
            yield OptionList(id="picker_list")
        yield Static("", id="footer_hints")

    def on_mount(self) -> None:
        self._choices = (
            list(self._item.choices_provider(self.app))  # type: ignore[arg-type]
            if self._item.choices_provider
            else []
        )
        current = (
            self._item.picker_getter(self.app) if self._item.picker_getter else None  # type: ignore[arg-type]
        )
        if self._item.multi:
            self._selected = set(current) if isinstance(current, list | tuple | set) else set()
        else:
            self._selected = {current} if current not in (None, "") else set()
        self._render_options()
        self.query_one("#picker_list", OptionList).focus()
        self._render_footer()

    def _render_footer(self) -> None:
        app: AcornApp = self.app  # type: ignore[assignment]
        hints: tuple[tuple[str, str], ...] = (
            (("⏎", "Toggle"), ("Esc", "Save"))
            if self._item.multi
            else (("⏎", "Select"), ("Esc", "Cancel"))
        )
        self.query_one("#footer_hints", Static).update(_hint_bar(app, hints))

    def _render_options(self) -> None:
        lst = self.query_one("#picker_list", OptionList)
        lst.clear_options()
        if not self._choices:
            lst.add_option(Option(Text("(no options)", style="dim"), disabled=True))
            return
        for c in self._choices:
            marker = "✓" if c.value in self._selected else " "
            t = Text(f"[{marker}] {c.label}")
            if c.description:
                t.append(f"   {c.description}", style="dim")
            lst.add_option(Option(t, id=str(c.value)))

    @on(OptionList.OptionSelected, "#picker_list")
    def _on_selected(self, ev: OptionList.OptionSelected) -> None:
        if ev.option.id is None:
            return
        target = next((c for c in self._choices if str(c.value) == ev.option.id), None)
        if target is None:
            return
        if self._item.multi:
            if target.value in self._selected:
                self._selected.discard(target.value)
            else:
                self._selected.add(target.value)
            self._render_options()
            return
        self._commit({target.value})
        self.app.pop_screen()

    def action_back(self) -> None:
        if self._item.multi:
            self._commit(self._selected)
        self.app.pop_screen()

    def action_cursor(self, direction: int) -> None:
        lst = self.query_one("#picker_list", OptionList)
        if direction > 0:
            lst.action_cursor_down()
        else:
            lst.action_cursor_up()

    def action_activate(self) -> None:
        self.query_one("#picker_list", OptionList).action_select()

    def _commit(self, values: set[Any]) -> None:
        if self._item.picker_setter is None:
            return
        try:
            if self._item.multi:
                self._item.picker_setter(self.app, sorted(values))  # type: ignore[arg-type]
            else:
                v = next(iter(values), None)
                if v is not None:
                    self._item.picker_setter(self.app, v)  # type: ignore[arg-type]
        except Exception as e:
            self.notify(_summarize(e), severity="error", title="Save failed")


# ── Collection-form screens (rebuilt from CollectionsScreen) ────────


class SourceFormScreen(Screen[None]):
    """Per-source editor.

    Multi-field form (Path, Includes, Excludes, Filter, Follow symlinks)
    plus a TextArea below for the pasted-frontmatter sample tester. Same
    chrome as the other Settings screens.
    """

    BINDINGS = [  # noqa: RUF012
        Binding("escape,left", "back", "Back", show=False),
        Binding("tab", "cycle_focus(1)", show=False),
        Binding("shift+tab", "cycle_focus(-1)", show=False),
        Binding("ctrl+s", "save_close", show=False),
    ]

    CSS = """
    SourceFormScreen { background: $surface; }
    SourceFormScreen > #settings_box {
        height: 1fr;
        border: round $primary 50%;
        padding: 0 1;
    }
    SourceFormScreen > #settings_box:focus-within { border: round $accent; }
    SourceFormScreen #frontmatter_sample {
        height: 8; border: round $primary 50%; padding: 0 1;
    }
    SourceFormScreen #frontmatter_sample:focus { border: round $accent; }
    SourceFormScreen .form_separator { color: $text-muted; padding: 1 0 0 0; }
    SourceFormScreen #match_status { color: $text-muted; padding: 0 0 0 0; }
    SourceFormScreen #match_status.-match { color: $success; }
    SourceFormScreen #match_status.-no-match { color: $error; }
    SourceFormScreen > #footer_hints {
        dock: bottom; height: 1; background: $surface; padding: 0 1; color: $text-muted;
    }
    """

    def __init__(self, *, collection_name: str, source_index: int | None) -> None:
        super().__init__()
        self._collection_name = collection_name
        self._source_index = source_index  # None = adding new
        self._fields: dict[str, Any] = {
            "path": "",
            "includes": "",
            "excludes": "",
            "filter": "",
            "follow_symlinks": False,
        }
        # Snapshot the current source (if editing) for cancel and the
        # "needs reindex on save" check.
        self._snapshot: dict[str, Any] = {}

    def compose(self) -> ComposeResult:
        title = (
            f"Collections › {self._collection_name} › Sources › Source {(self._source_index or 0) + 1}"
            if self._source_index is not None
            else f"Collections › {self._collection_name} › Sources › New source"
        )
        with Vertical(id="settings_box") as box:
            box.border_title = title
            yield SettingsList()
            yield Static(
                "─── Test filter against sample frontmatter ─────────────",
                classes="form_separator",
            )
            yield TextArea("", id="frontmatter_sample")
            yield Static("(no sample)", id="match_status")
        yield EditBar()
        yield Static("Ctrl+S Save · Esc Cancel", id="form_help")
        yield Static("", id="footer_hints")

    # ── Lifecycle ──────────────────────────────────────────────

    def on_mount(self) -> None:
        self._load_snapshot()
        self._populate_fields()
        self._render_footer()
        self.query_one(SettingsList).focus()

    def _load_snapshot(self) -> None:
        app: AcornApp = self.app  # type: ignore[assignment]
        cfg = app._config  # type: ignore[attr-defined]
        if (
            cfg is None
            or self._collection_name not in cfg.collections
            or self._source_index is None
        ):
            return
        sources = cfg.collections[self._collection_name].sources
        if not (0 <= self._source_index < len(sources)):
            return
        s = sources[self._source_index]
        self._fields = {
            "path": str(s.path),
            "includes": ", ".join(s.includes),
            "excludes": ", ".join(s.excludes),
            "filter": s.frontmatter_filter or "",
            "follow_symlinks": bool(s.follow_symlinks),
        }
        self._snapshot = dict(self._fields)

    def _populate_fields(self) -> None:
        self.query_one(SettingsList).set_items(self._build_field_items())

    def _build_field_items(self) -> list[MenuItem]:
        return [
            self._field_item("path", "Path", hint="path or ~/path"),
            self._field_item("includes", "Includes", hint="comma-separated globs"),
            self._field_item("excludes", "Excludes", hint="comma-separated globs"),
            self._field_item("filter", "Filter", hint="frontmatter DSL"),
            MenuItem(
                id="form.follow_symlinks",
                label="Follow symlinks",
                kind=KIND_TOGGLE,
                toggle_getter=lambda _app: bool(self._fields["follow_symlinks"]),
                toggle_setter=lambda _app, v: self._set_follow(v),
            ),
        ]

    def _field_item(self, key: str, label: str, *, hint: str) -> MenuItem:
        def _get(_app: Any) -> str:
            v = self._fields[key]
            if key == "filter" and v:
                status = self._parse_status(v)
                return f"{v}   {status}".rstrip()
            return v or "(unset)"

        return MenuItem(
            id=f"form.{key}",
            label=label,
            kind=KIND_SCALAR,
            setting_path="",  # we write into self._fields, not config.toml
            hint=hint,
            coerce=str,
            value_getter=_get,
        )

    def _set_follow(self, value: bool) -> None:
        self._fields["follow_symlinks"] = bool(value)

    # ── Field editing via the shared edit-bar ─────────────────

    @on(SettingsList.Activated)
    def _on_field_activated(self, ev: SettingsList.Activated) -> None:
        item = ev.item
        if item.kind == KIND_SCALAR:
            current = self._fields.get(item.id.split(".", 1)[-1], "")
            if item.id == "form.filter":
                current = self._fields["filter"]
            self.query_one(EditBar).open(item, str(current or ""))
        elif item.kind == KIND_TOGGLE:
            new = not (item.toggle_getter(self.app) if item.toggle_getter else False)  # type: ignore[arg-type]
            if item.toggle_setter is not None:
                item.toggle_setter(self.app, new)  # type: ignore[arg-type]
            self.query_one(SettingsList).refresh_values()

    @on(EditBar.EditCommitted)
    def _on_edit_committed(self, ev: EditBar.EditCommitted) -> None:
        field_key = ev.item.id.split(".", 1)[-1]
        if field_key == "filter":
            # Validate DSL before accepting.
            text = str(ev.value or "").strip()
            if text:
                from acorn.filter_dsl import parse_or_error

                _pred, err = parse_or_error(text)
                if err is not None:
                    self.query_one(EditBar).show_error(f"col {err.column}: {err.message}")
                    return
        self._fields[field_key] = ev.value
        self.query_one(EditBar).close()
        self.query_one(SettingsList).refresh_values()
        self.query_one(SettingsList).focus()
        self._refresh_match_status()

    # ── Match status (frontmatter sample tester) ──────────────

    @on(TextArea.Changed, "#frontmatter_sample")
    def _on_sample_changed(self, _ev: TextArea.Changed) -> None:
        self._refresh_match_status()

    def _refresh_match_status(self) -> None:
        sample = self.query_one("#frontmatter_sample", TextArea).text
        filter_text = str(self._fields["filter"] or "").strip()
        status = self.query_one("#match_status", Static)
        status.remove_class("-match")
        status.remove_class("-no-match")
        if not sample.strip():
            status.update("(no sample)")
            return
        from acorn.filter_dsl import parse_or_error
        from acorn.frontmatter import FrontmatterParseError, read_frontmatter_from_text

        try:
            fm: dict[str, object] = read_frontmatter_from_text(sample) or {}
        except FrontmatterParseError as e:
            status.update(f"✗ frontmatter parse error: {e}")
            status.add_class("-no-match")
            return
        if not filter_text:
            status.update("(no filter)")
            return
        pred, err = parse_or_error(filter_text)
        if err is not None or pred is None:
            status.update(f"✗ filter syntax: col {err.column}" if err else "✗ syntax error")
            status.add_class("-no-match")
            return
        if pred(fm):
            status.update("✓ sample matches filter")
            status.add_class("-match")
        else:
            status.update("✗ sample does not match filter")
            status.add_class("-no-match")

    def _parse_status(self, filter_text: str) -> str:
        from acorn.filter_dsl import parse_or_error

        text = filter_text.strip()
        if not text:
            return ""
        _pred, err = parse_or_error(text)
        if err is None:
            return "✓"
        return f"✗ col {err.column}"

    # ── Footer ────────────────────────────────────────────────

    def _render_footer(self) -> None:
        app: AcornApp = self.app  # type: ignore[assignment]
        self.query_one("#footer_hints", Static).update(
            _hint_bar(
                app,
                (
                    ("Tab", "Fields ↔ sample"),
                    ("⏎", "Edit"),
                    ("Ctrl+S", "Save"),
                    ("Esc", "Cancel"),
                ),
            )
        )

    # ── Save / cancel ────────────────────────────────────────

    def action_save_close(self) -> None:
        from pathlib import Path

        from acorn.config import (
            CollectionConfig,
            SourceConfig,
            default_config_path,
            load,
            write_collection,
        )

        path = str(self._fields["path"] or "").strip().strip("'\"")
        if not path:
            self.notify("Path is required", severity="error", title="Invalid source")
            return
        if not Path(path).expanduser().exists():
            self.notify(
                f"Path does not exist: {path}",
                severity="error",
                title="Invalid source",
            )
            return
        try:
            new_source = SourceConfig(
                path=Path(path),
                includes=[s.strip() for s in str(self._fields["includes"]).split(",") if s.strip()],
                excludes=[s.strip() for s in str(self._fields["excludes"]).split(",") if s.strip()],
                follow_symlinks=bool(self._fields["follow_symlinks"]),
                frontmatter_filter=(str(self._fields["filter"]) or None),
            )
        except Exception as e:
            self.notify(_summarize(e), severity="error", title="Invalid source")
            return

        app: AcornApp = self.app  # type: ignore[assignment]
        cfg = app._config  # type: ignore[attr-defined]
        if cfg is None or self._collection_name not in cfg.collections:
            self.notify("Collection vanished — please reopen the menu", severity="error")
            return
        col: CollectionConfig = cfg.collections[self._collection_name]
        if self._source_index is None:
            col.sources.append(new_source)
        else:
            col.sources[self._source_index] = new_source
        try:
            write_collection(
                config_path=default_config_path(),
                name=self._collection_name,
                collection=col,
            )
        except Exception as e:
            self.notify(_summarize(e), severity="error", title="Save failed")
            return
        app._config = load()  # type: ignore[attr-defined]
        app._refresh_collections_panel()  # type: ignore[attr-defined]
        # Trigger a reindex if the source set materially changed.
        if self._snapshot != self._fields or self._source_index is None:
            app._reindex_collection_async(self._collection_name)  # type: ignore[attr-defined]
        self.app.pop_screen()

    def action_back(self) -> None:
        self.app.pop_screen()

    # ── Tab cycles field list ↔ sample TextArea ───────────────

    def action_cycle_focus(self, direction: int) -> None:
        widgets = [self.query_one(SettingsList), self.query_one("#frontmatter_sample", TextArea)]
        focused = self.focused
        # Find current index (default: 0 if not in list).
        idx = 0
        for i, w in enumerate(widgets):
            if focused is w or (focused is not None and focused in w.walk_children()):
                idx = i
                break
        target = widgets[(idx + direction) % len(widgets)]
        target.focus()


class AddCollectionWizard(Screen[None]):
    """Single-screen form for creating a new collection + its first source.

    Field rows live in a SettingsList; the frontmatter sample tester docks
    below. Ctrl+S validates everything and writes via write_collection +
    triggers a reindex.
    """

    BINDINGS = [  # noqa: RUF012
        Binding("escape,left", "back", "Cancel", show=False),
        Binding("ctrl+s", "save_close", "Save", show=False),
        Binding("tab", "cycle_focus(1)", show=False),
        Binding("shift+tab", "cycle_focus(-1)", show=False),
    ]

    CSS = """
    AddCollectionWizard { background: $surface; align: center middle; }
    AddCollectionWizard > #settings_box {
        height: auto;
        max-height: 90%;
        width: auto;
        min-width: 72;
        max-width: 100;
        border: round $primary 50%;
        padding: 0 1;
    }
    AddCollectionWizard > #settings_box:focus-within { border: round $accent; }
    AddCollectionWizard #frontmatter_sample {
        height: 6; border: round $primary 50%; padding: 0 1;
    }
    AddCollectionWizard #frontmatter_sample:focus { border: round $accent; }
    AddCollectionWizard .form_separator { color: $text-muted; padding: 1 0 0 0; }
    AddCollectionWizard #match_status { color: $text-muted; }
    AddCollectionWizard #match_status.-match { color: $success; }
    AddCollectionWizard #match_status.-no-match { color: $error; }
    AddCollectionWizard > #footer_hints {
        dock: bottom; height: 1; background: $surface; padding: 0 1; color: $text-muted;
    }
    """

    def __init__(self) -> None:
        super().__init__()
        from acorn.config import EXCLUDES_PRESETS

        self._fields: dict[str, Any] = {
            "name": "",
            "path": "",
            "includes": [],
            "includes_custom": "",
            "excludes_presets": [
                key for key, preset in EXCLUDES_PRESETS.items() if preset["default"]
            ],
            "excludes_custom": "",
            "filter": "",
            "follow_symlinks": False,
        }

    def compose(self) -> ComposeResult:
        with Vertical(id="settings_box") as box:
            box.border_title = "Add Collection"
            yield SettingsList()
            yield Static(
                "─── Test filter against sample frontmatter ───",
                classes="form_separator",
            )
            yield TextArea("", id="frontmatter_sample")
            yield Static("(no sample)", id="match_status")
            yield DetailStrip()
        yield EditBar()
        yield Static("", id="footer_hints")

    def on_mount(self) -> None:
        self._populate_fields()
        self.query_one(SettingsList).focus()
        app: AcornApp = self.app  # type: ignore[assignment]
        self.query_one("#footer_hints", Static).update(
            _hint_bar(
                app,
                (
                    ("⏎", "Edit"),
                    ("Tab", "Sample"),
                    ("Ctrl+S", "Save & Index"),
                    ("Esc", "Cancel"),
                ),
            )
        )

    def _populate_fields(self) -> None:
        self.query_one(SettingsList).set_items(self._build_field_items())

    def _build_field_items(self) -> list[MenuItem]:
        from acorn.config import EXCLUDES_PRESETS, INDEXER_FILETYPES

        return [
            MenuItem(
                id="wiz.name",
                label="Name",
                kind=KIND_SCALAR,
                value_getter=lambda _app: self._fields["name"] or "(required)",
            ),
            MenuItem(
                id="wiz.path",
                label="Source path",
                kind=KIND_SCALAR,
                value_getter=lambda _app: self._fields["path"] or "(required)",
            ),
            MenuItem(
                id="wiz.includes",
                label="Includes",
                kind=KIND_PICKER,
                multi=True,
                choices_provider=lambda _app: [
                    *(
                        ChoiceOption(value=ext, label=label)
                        for ext, label in INDEXER_FILETYPES.items()
                    ),
                    ChoiceOption(
                        value="__custom__",
                        label="Custom glob…",
                        description="Add a free-form glob pattern (e.g. `**/*.org`).",
                    ),
                ],
                picker_getter=lambda _app: self._includes_picker_state(),
                picker_setter=lambda _app, vs: self._set_includes(vs),
            ),
            MenuItem(
                id="wiz.excludes",
                label="Excludes",
                kind=KIND_PICKER,
                multi=True,
                choices_provider=lambda _app: [
                    *(
                        ChoiceOption(
                            value=key,
                            label=preset["label"],
                            description=", ".join(preset["globs"]),
                        )
                        for key, preset in EXCLUDES_PRESETS.items()
                    ),
                    ChoiceOption(
                        value="__custom__",
                        label="Custom glob…",
                        description="Add a free-form glob pattern (comma-separated).",
                    ),
                ],
                picker_getter=lambda _app: self._excludes_picker_state(),
                picker_setter=lambda _app, vs: self._set_excludes_presets(vs),
            ),
            MenuItem(
                id="wiz.filter",
                label="Frontmatter filter",
                kind=KIND_SCALAR,
                value_getter=lambda _app: self._fields["filter"] or "(none)",
            ),
            MenuItem(
                id="wiz.follow_symlinks",
                label="Follow symlinks",
                kind=KIND_TOGGLE,
                toggle_getter=lambda _app: bool(self._fields["follow_symlinks"]),
                toggle_setter=lambda _app, v: self._set_follow(v),
            ),
        ]

    def _summarize_includes(self) -> str:
        return f"{len(self._fields['includes'])} types"

    def _summarize_excludes(self) -> str:
        return f"{len(self._fields['excludes_presets'])} presets"

    def _set_follow(self, value: bool) -> None:
        self._fields["follow_symlinks"] = bool(value)

    def _includes_picker_state(self) -> list[str]:
        """What the Includes picker should show as pre-selected — the
        extension list, plus the `__custom__` sentinel when a custom
        value is set so the user sees the toggle as ticked."""
        state = list(self._fields["includes"])
        if str(self._fields.get("includes_custom") or "").strip():
            state.append("__custom__")
        return state

    def _excludes_picker_state(self) -> list[str]:
        state = list(self._fields["excludes_presets"])
        if str(self._fields.get("excludes_custom") or "").strip():
            state.append("__custom__")
        return state

    def _set_includes(self, values: list[str]) -> None:
        """Splits the picker output into preset extensions vs the custom
        sentinel. When `__custom__` is in the selection we leave the
        existing ``includes_custom`` value (or trigger a follow-up edit
        bar) so the user can type their glob."""
        picked = list(values)
        wants_custom = "__custom__" in picked
        exts = [v for v in picked if v != "__custom__"]
        self._fields["includes"] = exts
        if wants_custom and not str(self._fields.get("includes_custom") or "").strip():
            # Open an EditBar to prompt for the custom glob value.
            self._prompt_custom("includes_custom", "Includes custom glob")
        elif not wants_custom:
            self._fields["includes_custom"] = ""
        self.query_one(SettingsList).refresh_values()

    def _set_excludes_presets(self, values: list[str]) -> None:
        picked = list(values)
        wants_custom = "__custom__" in picked
        presets = [v for v in picked if v != "__custom__"]
        self._fields["excludes_presets"] = presets
        if wants_custom and not str(self._fields.get("excludes_custom") or "").strip():
            self._prompt_custom("excludes_custom", "Excludes custom globs (comma-separated)")
        elif not wants_custom:
            self._fields["excludes_custom"] = ""
        self.query_one(SettingsList).refresh_values()

    def _prompt_custom(self, field_key: str, label: str) -> None:
        """Open the wizard's EditBar to capture a custom glob value and
        store it in ``self._fields[field_key]`` on submit."""
        item = MenuItem(
            id=f"wiz.{field_key}",
            label=label,
            kind=KIND_SCALAR,
            value_getter=lambda _app, key=field_key: str(self._fields.get(key) or ""),
        )
        self.query_one(EditBar).open(item, str(self._fields.get(field_key) or ""))

    @on(SettingsList.Activated)
    def _on_field_activated(self, ev: SettingsList.Activated) -> None:
        item = ev.item
        if item.kind == KIND_PICKER:
            self.app.push_screen(PickerScreen(item))
        elif item.kind == KIND_SCALAR:
            field_key = item.id.split(".", 1)[-1]
            current = self._fields.get(field_key, "")
            self.query_one(EditBar).open(item, str(current or ""))
        elif item.kind == KIND_TOGGLE:
            new = not (item.toggle_getter(self.app) if item.toggle_getter else False)  # type: ignore[arg-type]
            if item.toggle_setter is not None:
                item.toggle_setter(self.app, new)  # type: ignore[arg-type]
            self.query_one(SettingsList).refresh_values()

    @on(EditBar.EditCommitted)
    def _on_edit_committed(self, ev: EditBar.EditCommitted) -> None:
        field_key = ev.item.id.split(".", 1)[-1]
        self._fields[field_key] = ev.value
        self.query_one(EditBar).close()
        self.query_one(SettingsList).refresh_values()
        self.query_one(SettingsList).focus()

    def action_back(self) -> None:
        self.app.pop_screen()

    def action_save_close(self) -> None:
        from pathlib import Path

        from acorn.config import (
            EXCLUDES_PRESETS,
            CollectionConfig,
            SourceConfig,
            default_config_path,
            load,
            write_collection,
        )

        name = str(self._fields["name"]).strip()
        path = str(self._fields["path"]).strip().strip("'\"")
        if not name:
            self.notify("Name is required", severity="error")
            return
        if not path:
            self.notify("Source path is required", severity="error")
            return
        p = Path(path).expanduser()
        if not p.exists():
            self.notify(f"Path does not exist: {p}", severity="error")
            return

        includes_globs: list[str] = [f"**/*.{ext}" for ext in self._fields["includes"]]
        includes_custom = str(self._fields.get("includes_custom") or "")
        for g in includes_custom.split(","):
            g = g.strip()
            if g:
                includes_globs.append(g)

        excludes_globs: list[str] = []
        for preset_id in self._fields["excludes_presets"]:
            excludes_globs.extend(EXCLUDES_PRESETS[preset_id]["globs"])
        custom = str(self._fields["excludes_custom"] or "")
        for g in custom.split(","):
            g = g.strip()
            if g:
                excludes_globs.append(g)

        app: AcornApp = self.app  # type: ignore[assignment]
        cfg = app._config  # type: ignore[attr-defined]
        if cfg is not None and name in cfg.collections:
            self.notify(f"Collection {name!r} already exists", severity="warning")
            return

        source = SourceConfig(
            path=p,
            includes=includes_globs,
            excludes=excludes_globs,
            follow_symlinks=bool(self._fields["follow_symlinks"]),
            frontmatter_filter=(str(self._fields["filter"]).strip() or None),
        )
        new_collection = CollectionConfig(sources=[source])
        config_path = default_config_path()
        config_path.parent.mkdir(parents=True, exist_ok=True)
        write_collection(
            config_path=config_path,
            name=name,
            collection=new_collection,
        )
        app._config = load()  # type: ignore[attr-defined]
        app._refresh_collections_panel()  # type: ignore[attr-defined]
        app._reindex_collection_async(name)  # type: ignore[attr-defined]
        # Pop wizard, then push the new collection's per-collection sub-screen.
        self.app.pop_screen()
        from acorn.tui.menu import _make_open_collection_screen

        _make_open_collection_screen(name)(app)

    def action_cycle_focus(self, direction: int) -> None:
        widgets = [
            self.query_one(SettingsList),
            self.query_one("#frontmatter_sample", TextArea),
        ]
        focused = self.focused
        idx = 0
        for i, w in enumerate(widgets):
            if focused is w or (focused is not None and focused in w.walk_children()):
                idx = i
                break
        widgets[(idx + direction) % len(widgets)].focus()


class NewCollectionScreen(Screen[None]):
    """Tiny one-Input prompt for creating an empty collection."""

    BINDINGS = [  # noqa: RUF012
        Binding("escape,left", "back", "Cancel", show=False),
    ]

    CSS = """
    NewCollectionScreen { background: $surface; }
    NewCollectionScreen > #settings_box {
        height: auto;
        border: round $primary 50%;
        padding: 0 1;
        margin: 1 4;
    }
    NewCollectionScreen > #settings_box:focus-within { border: round $accent; }
    NewCollectionScreen > #footer_hints {
        dock: bottom; height: 1; background: $surface; padding: 0 1; color: $text-muted;
    }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="settings_box") as box:
            box.border_title = "Collections › New collection"
            yield Input(placeholder="Collection name (e.g. research)", id="new_collection_name")
        yield Static("", id="footer_hints")

    def on_mount(self) -> None:
        self.query_one("#new_collection_name", Input).focus()
        self._render_footer()

    def _render_footer(self) -> None:
        app: AcornApp = self.app  # type: ignore[assignment]
        self.query_one("#footer_hints", Static).update(
            _hint_bar(app, (("⏎", "Create"), ("Esc", "Cancel")))
        )

    @on(Input.Submitted, "#new_collection_name")
    def _create(self, ev: Input.Submitted) -> None:
        name = ev.value.strip()
        if not name:
            self.app.pop_screen()
            return
        from acorn.config import CollectionConfig, default_config_path, load, write_collection

        app: AcornApp = self.app  # type: ignore[assignment]
        if app._config and name in app._config.collections:  # type: ignore[attr-defined]
            self.notify(f"Collection {name!r} already exists.", severity="warning")
            return
        write_collection(
            config_path=default_config_path(),
            name=name,
            collection=CollectionConfig(sources=[]),
        )
        app._config = load()  # type: ignore[attr-defined]
        app._refresh_collections_panel()  # type: ignore[attr-defined]
        self.app.pop_screen()

    def action_back(self) -> None:
        self.app.pop_screen()


class RenameCollectionScreen(Screen[None]):
    """Tiny one-Input prompt for renaming a collection.

    Implementation note: there is no atomic "rename collection" in
    `acorn.config`, so this writes the new name (copy of the existing
    collection) then deletes the old. Reindex follows because the
    on-disk index keys chunks by collection name.
    """

    BINDINGS = [  # noqa: RUF012
        Binding("escape,left", "back", "Cancel", show=False),
    ]

    CSS = NewCollectionScreen.CSS  # share styling

    def __init__(self, *, collection_name: str) -> None:
        super().__init__()
        self._old_name = collection_name

    def compose(self) -> ComposeResult:
        with Vertical(id="settings_box") as box:
            box.border_title = f"Collections › {self._old_name} › Rename"
            yield Input(value=self._old_name, id="new_collection_name")
        yield Static("", id="footer_hints")

    def on_mount(self) -> None:
        self.query_one("#new_collection_name", Input).focus()
        app: AcornApp = self.app  # type: ignore[assignment]
        self.query_one("#footer_hints", Static).update(
            _hint_bar(app, (("⏎", "Save"), ("Esc", "Cancel")))
        )

    @on(Input.Submitted, "#new_collection_name")
    def _save(self, ev: Input.Submitted) -> None:
        new_name = ev.value.strip()
        if not new_name or new_name == self._old_name:
            self.app.pop_screen()
            return
        from acorn.config import (
            default_config_path,
            delete_collection,
            load,
            write_collection,
        )

        app: AcornApp = self.app  # type: ignore[assignment]
        cfg = app._config  # type: ignore[attr-defined]
        if cfg is None or self._old_name not in cfg.collections:
            self.notify("Collection vanished", severity="error")
            self.app.pop_screen()
            return
        if new_name in cfg.collections:
            self.notify(f"{new_name!r} already exists", severity="warning")
            return
        existing = cfg.collections[self._old_name]
        write_collection(
            config_path=default_config_path(),
            name=new_name,
            collection=existing,
        )
        delete_collection(config_path=default_config_path(), name=self._old_name)
        app._config = load()  # type: ignore[attr-defined]
        app._refresh_collections_panel()  # type: ignore[attr-defined]
        app._reindex_collection_async(new_name)  # type: ignore[attr-defined]
        # Pop twice — past Rename and the now-stale per-collection screen.
        self.app.pop_screen()
        self.app.pop_screen()

    def action_back(self) -> None:
        self.app.pop_screen()


class DeleteCollectionScreen(Screen[None]):
    """Confirm + execute deletion of a collection, including dropping
    its chunks from the on-disk index."""

    BINDINGS = [  # noqa: RUF012
        Binding("escape,left", "back", "Cancel", show=False),
        Binding("up,k", "cursor(-1)", show=False),
        Binding("down,j", "cursor(1)", show=False),
        Binding("enter", "activate", show=False),
    ]

    CSS = """
    DeleteCollectionScreen { background: $surface; }
    DeleteCollectionScreen > #settings_box {
        height: auto;
        border: round $error;
        padding: 0 1;
        margin: 1 4;
    }
    DeleteCollectionScreen > #settings_box:focus-within { border: round $error; }
    DeleteCollectionScreen #confirm_list { height: auto; }
    DeleteCollectionScreen > #footer_hints {
        dock: bottom; height: 1; background: $surface; padding: 0 1; color: $text-muted;
    }
    DeleteCollectionScreen .warning { color: $warning; padding: 0 0 1 0; }
    """

    def __init__(self, *, collection_name: str) -> None:
        super().__init__()
        self._name = collection_name

    def compose(self) -> ComposeResult:
        with Vertical(id="settings_box") as box:
            box.border_title = f"Collections › {self._name} › Delete"
            yield Static(
                f"Delete collection {self._name!r}?  This removes it from "
                "config.toml AND drops its chunks from the index.",
                classes="warning",
            )
            opts = OptionList(
                Option(Text(f"Yes, delete {self._name}", style="bold"), id="yes"),
                Option("Cancel", id="no"),
                id="confirm_list",
            )
            yield opts
        yield Static("", id="footer_hints")

    def on_mount(self) -> None:
        self.query_one("#confirm_list", OptionList).focus()
        app: AcornApp = self.app  # type: ignore[assignment]
        self.query_one("#footer_hints", Static).update(
            _hint_bar(app, (("⏎", "Confirm"), ("Esc", "Cancel")))
        )

    def action_cursor(self, direction: int) -> None:
        lst = self.query_one("#confirm_list", OptionList)
        if direction > 0:
            lst.action_cursor_down()
        else:
            lst.action_cursor_up()

    def action_activate(self) -> None:
        self.query_one("#confirm_list", OptionList).action_select()

    @on(OptionList.OptionSelected, "#confirm_list")
    def _on_select(self, ev: OptionList.OptionSelected) -> None:
        if ev.option.id == "no":
            self.app.pop_screen()
            return
        # Yes branch.
        from acorn.config import default_config_path, delete_collection, load
        from acorn.index import _ensure_index
        from acorn.schema import F_COLLECTION

        app: AcornApp = self.app  # type: ignore[assignment]
        delete_collection(config_path=default_config_path(), name=self._name)
        app._config = load()  # type: ignore[attr-defined]
        try:
            index = _ensure_index(app._index_dir)  # type: ignore[attr-defined]
            writer = index.writer(heap_size=50_000_000)
            writer.delete_documents(F_COLLECTION, self._name)
            writer.commit()
            writer.wait_merging_threads()
        except Exception as e:
            self.notify(f"Index drop failed: {e}", severity="error")
        app._refresh_collections_panel()  # type: ignore[attr-defined]
        # Pop Delete screen AND the now-stale per-collection screen.
        self.app.pop_screen()
        self.app.pop_screen()

    def action_back(self) -> None:
        self.app.pop_screen()


# ── Public entry points used by the main app ────────────────────────


def open_settings(app: AcornApp) -> None:
    """Open the Settings root menu — a short list of categories the
    user can drill into. No content stacked on a single screen."""
    items = build_root_items(app)
    app.push_screen(
        SettingsScreen(
            breadcrumb=(),
            items=items,
            root_provider=build_root_items,
        )
    )


def open_settings_section(app: AcornApp, section_id: str) -> None:
    """Push a Settings sub-screen directly (no intermediate root push).

    Used by drill-in rows on the root menu AND by the global shortcuts
    (`?` → Keybindings, F3 → Collections) so the user lands in one
    push and Esc returns to the main app in one press.
    """
    label = section_label(section_id)
    items = section_items(app, section_id)
    app.push_screen(
        SettingsScreen(
            breadcrumb=(label,),
            items=items,
            root_provider=build_root_items,
        )
    )
