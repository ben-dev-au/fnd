"""Settings & Commands menu — rendering and dispatch (Phase 2).

The menu's *data* lives in :mod:`fnd.tui.menu`. This module renders it
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
    rendered by :func:`fnd.tui.app.render_hint_bar` so the visual is
    identical to the main app's footer.

Drilling into a collection / source / picker pushes another
``Screen`` onto Textual's ``screen_stack``. ``Esc`` pops one level
naturally; no pre-popping or manual back stacks.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from rich.text import Text
from textual import events, on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.reactive import reactive
from textual.screen import Screen
from textual.widget import Widget
from textual.widgets import Input, OptionList, Static, TextArea
from textual.widgets.option_list import Option

from fnd.tui.menu import (
    KIND_ACTION,
    KIND_DISPLAY,
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
from fnd.tui.widgets import DetailStrip
from fnd.tui.widgets.toggle_tree import ToggleGroup, ToggleItem, ToggleTree

if TYPE_CHECKING:
    from fnd.tui.app import FNDApp


# Width budget for key column in Keys & Actions rows. Anything wider gets
# truncated rather than pushing the description.
_KEY_COL = 12


def _hint_bar(app: FNDApp, contextual: tuple[tuple[str, str], ...]) -> Any:
    """Build the shared hint-bar Text for a Settings screen. Anchors
    come from the main app (single source of truth)."""
    from fnd.tui.app import render_hint_bar

    return render_hint_bar(app._FOOTER_ANCHORS, contextual)  # type: ignore[attr-defined]


_SETTINGS_HINTS: tuple[tuple[str, str], ...] = (
    ("↑↓", "Nav"),
    ("⏎", "Open"),
    ("←", "Back"),
    ("/", "Filter"),
    ("Esc", "Back"),
)


# ── Confirm-screen helpers (Phase E) ────────────────────────────────


# Severity → (verb colour, CSS modifier class). Used by every confirm
# screen so colour is consistent end-to-end.
_CONFIRM_SAFE = ("bold green", "-safe")
_CONFIRM_RECOVERABLE = ("bold yellow", "-recoverable")
_CONFIRM_DESTRUCTIVE = ("bold red", "-destructive")


def build_confirm_body(
    *,
    outcome: str,
    cost: str,
    safety: str,
    irreversible: bool = False,
    outcome_label: str = "Outcome",
    cost_label: str = "Cost",
    safety_label: str = "Safety",
) -> Text:
    """Three-row labelled body shared by every confirm screen.

    Default labels (Outcome / Cost / Safety) describe a pay-then-gain
    action — installing, deleting, paying CPU to rebuild. Some actions
    don't fit that framing — uninstalling FREES disk rather than
    costing it. Callers override the labels (e.g. ``cost_label="Disk
    freed"``) so each screen reads naturally.

    Labels render dim; values render in default text. When
    ``irreversible`` is set, appends a red "Cannot be undone" line
    below the rows."""
    # Pad labels to a consistent column so the values align in the
    # rendered output regardless of label length.
    width = max(len(outcome_label), len(cost_label), len(safety_label))
    text = Text()
    text.append(outcome_label.ljust(width) + "   ", style="dim")
    text.append(outcome + "\n")
    text.append(cost_label.ljust(width) + "   ", style="dim")
    text.append(cost + "\n")
    text.append(safety_label.ljust(width) + "   ", style="dim")
    text.append(safety)
    if irreversible:
        text.append("\n\n")
        text.append("⚠ Cannot be undone.", style="bold red")
    return text


def confirm_yes_option(label: str, severity: str = "safe") -> Option:
    """Construct the affirming OptionList row with severity-coloured verb.

    ``severity`` is ``"safe"`` / ``"recoverable"`` / ``"destructive"``.
    Cancel rows stay plain; only Yes carries the colour."""
    style = {
        "safe": _CONFIRM_SAFE[0],
        "recoverable": _CONFIRM_RECOVERABLE[0],
        "destructive": _CONFIRM_DESTRUCTIVE[0],
    }.get(severity, _CONFIRM_SAFE[0])
    return Option(Text(label, style=style), id="yes")


def confirm_border_class(severity: str) -> str:
    """CSS modifier class to add to the screen so the bordered box
    renders in the right severity colour."""
    return {
        "safe": _CONFIRM_SAFE[1],
        "recoverable": _CONFIRM_RECOVERABLE[1],
        "destructive": _CONFIRM_DESTRUCTIVE[1],
    }.get(severity, _CONFIRM_SAFE[1])


# ── Row rendering ────────────────────────────────────────────────────


# Per-kind glyph constants. All render in default macOS Terminal fonts
# (Menlo, SF Mono, Monaco) — verified safe.
_GLYPH_TOGGLE_ON = "✓ on"  # U+2713 + text
_GLYPH_TOGGLE_OFF = "✗ off"  # U+2717 + text
_GLYPH_DRILL = "▸"  # U+25B8 small triangle
_GLYPH_PICKER = "▾"  # U+25BE small caret
_GLYPH_EXTERNAL = "↗"  # U+2197 upper-right arrow


def _render_row(
    item: MenuItem,
    app: FNDApp | None,
    width: int | None = None,
    breadcrumb: tuple[str, ...] | None = None,
    highlight: str | None = None,
) -> Text:
    """Render one menu row as Rich Text with per-kind visual language.

    Layout (left to right):
      [key]  ↗?label  ……………… <trailing segments>

    Per-kind trailing affordance:

      KIND_TOGGLE     ✓ on (green) / ✗ off (red)
      KIND_ACTION     [ Run ] / [ Delete… ] (accent)
      KIND_SUBMENU    summary (dim) + ▸ (accent)
      KIND_EXTERNAL   drill: summary + ▸ ; external_app: path (dim), label gets leading ↗
      KIND_PICKER     value (bold) + ▾ (accent)
      KIND_SCALAR     value (bold)
      KIND_DISPLAY    value (bold) — and label rendered dim instead of bright

    ``app`` may be ``None`` for tests that don't construct a full app —
    in that case the trailing slot is skipped.

    ``breadcrumb`` is a tuple of section labels — when provided it is
    rendered instead of the normal trailing so the user knows which
    section each cross-section search result comes from.

    ``highlight`` is the active search query. When given and the label
    contains a case-insensitive match, that substring is rendered bold.
    """
    if item.kind == KIND_HEADER:
        return _render_header(item, width)

    text = Text()
    label_style = "dim" if item.kind == KIND_DISPLAY else None
    leading_used = 0

    if item.key:
        # Bracketed key in 12-char column: "[<key>]" + padding. Used by
        # the Keybindings cheat sheet only.
        bracket_open = Text("[", style="dim")
        key_glyph = Text(item.key, style="bold")
        bracket_close = Text("]", style="dim")
        key_field = bracket_open + key_glyph + bracket_close
        used = len(item.key) + 2
        key_field.append(" " * max(1, _KEY_COL - used))
        text.append_text(key_field)

    # External-app rows get a leading ↗ in $accent before the label.
    if item.kind == KIND_EXTERNAL and item.external_app:
        text.append(f"{_GLYPH_EXTERNAL} ", style="bold cyan")
        leading_used = 2  # glyph + space

    # Compute the trailing affordance up-front so the label can be
    # truncated with `…` when label + dots + affordance would exceed
    # the row width. Without this the action label (`[ Clear… ]`,
    # `[ Rebuild ]`, etc.) clips at the right border, hiding the
    # primary signal of "what Enter does."
    pending_segments = _trailing_segments(item, app) if not breadcrumb else []
    label_to_render = item.label
    if width is not None and pending_segments:
        affordance_len = sum(
            len(seg_text) for seg_text, seg_style in pending_segments if "dim" not in seg_style
        )
        used_leading = (_KEY_COL if item.key else 0) + leading_used
        # Minimum dotted pad + leading/trailing space around it.
        min_pad = 2
        gap = 2
        label_budget = width - used_leading - affordance_len - min_pad - gap
        if label_budget > 0 and len(label_to_render) > label_budget:
            keep = max(1, label_budget - 1)
            label_to_render = label_to_render[:keep] + "…"

    if highlight:
        low = label_to_render.lower()
        h_low = highlight.lower()
        i = low.find(h_low)
        if i >= 0:
            text.append(label_to_render[:i], style=label_style)
            text.append(label_to_render[i : i + len(highlight)], style="bold")
            text.append(label_to_render[i + len(highlight) :], style=label_style)
        else:
            text.append(label_to_render, style=label_style)
    else:
        text.append(label_to_render, style=label_style)

    if breadcrumb:
        bc_text = " › ".join(breadcrumb)
        if width is not None:
            used = (_KEY_COL if item.key else 0) + leading_used + len(item.label)
            pad = max(2, width - used - len(bc_text) - 2)
            text.append(" " + "·" * pad + " ", style="dim")
        else:
            text.append("   ")
        text.append(bc_text, style="dim italic")
        return text

    # Per-kind trailing segments (already computed above for the
    # label-budget pass).
    segments = pending_segments
    if not segments:
        return text

    used = (_KEY_COL if item.key else 0) + leading_used + len(label_to_render)
    if width is not None:
        # The trailing affordance (rightmost segment) is the row's
        # primary signal of "what does Enter do." If the row's total
        # render would exceed width, the terminal silently truncates
        # the right edge — losing the glyph the user needs. Truncate
        # the longest dim/summary segment instead, leaving room for at
        # least a 2-char dotted pad and the whole affordance.
        min_pad = 2
        gap = 2  # leading + trailing space around the dots
        segments = _truncate_segments_to_fit(segments, budget=width - used - min_pad - gap)
        plain_len = sum(len(seg_text) for seg_text, _ in segments)
        pad = max(min_pad, width - used - plain_len - gap)
        text.append(" " + "·" * pad + " ", style="dim")
    else:
        text.append("   ")
    for seg_text, seg_style in segments:
        text.append(seg_text, style=seg_style)
    return text


def _truncate_segments_to_fit(
    segments: list[tuple[str, str]], *, budget: int
) -> list[tuple[str, str]]:
    """Shrink the first dim/summary segment with ``…`` so the total
    fits within ``budget``. The trailing affordance segment (and any
    other non-dim segments) is preserved verbatim — losing the glyph
    would defeat the per-kind visual language.

    Returns the original list when no truncation is needed."""
    plain_len = sum(len(seg_text) for seg_text, _ in segments)
    if plain_len <= budget:
        return segments
    # Reserve every non-dim segment in full; truncate the leading
    # dim segments to consume whatever's left.
    reserved = sum(len(seg_text) for seg_text, style in segments if "dim" not in style)
    available_for_dim = budget - reserved
    if available_for_dim <= 1:
        # Pathologically narrow row — drop dim segments altogether,
        # keep only the affordance.
        return [(t, s) for t, s in segments if "dim" not in s]
    out: list[tuple[str, str]] = []
    consumed = 0
    truncated = False
    for seg_text, seg_style in segments:
        if "dim" in seg_style and not truncated:
            remaining = available_for_dim - consumed
            if len(seg_text) <= remaining:
                out.append((seg_text, seg_style))
                consumed += len(seg_text)
            else:
                # Truncate this segment with a single-char ellipsis.
                keep = max(0, remaining - 1)
                out.append((seg_text[:keep] + "…", seg_style))
                truncated = True
        else:
            out.append((seg_text, seg_style))
    return out


def _trailing_segments(item: MenuItem, app: FNDApp | None) -> list[tuple[str, str]]:
    """Per-kind trailing segments as (text, rich_style) pairs.

    Rich Text styles used:
      ``bold green``  — toggle on, safe affirmation
      ``bold red``    — toggle off, destructive
      ``bold cyan``   — accent: action brackets, drill arrow, picker caret, ↗
      ``bold``        — bright value (scalar / picker value / display value)
      ``dim``         — drill row summary text, parenthetical context
    """
    if app is None:
        return []

    if item.kind == KIND_TOGGLE and item.toggle_getter is not None:
        try:
            on = bool(item.toggle_getter(app))
        except Exception:
            on = False
        return [(_GLYPH_TOGGLE_ON, "bold green") if on else (_GLYPH_TOGGLE_OFF, "bold red")]

    if item.kind == KIND_ACTION:
        # Keybindings cheat-sheet rows carry a ``key`` glyph in their
        # leading column — that IS the affordance. A trailing button
        # would (a) repeat noise across ~30 rows of documentation and
        # (b) push the leading [key] into the right margin under
        # narrow widths.
        if item.key:
            return []
        return [(f"[ {item.action_label} ]", "bold cyan")]

    if item.kind == KIND_SUBMENU:
        summary = ""
        if item.value_getter is not None:
            try:
                summary = item.value_getter(app) or ""
            except Exception:
                summary = ""
        if summary:
            return [(summary + " ", "dim"), (_GLYPH_DRILL, "bold cyan")]
        return [(_GLYPH_DRILL, "bold cyan")]

    if item.kind == KIND_EXTERNAL:
        summary = ""
        if item.value_getter is not None:
            try:
                summary = item.value_getter(app) or ""
            except Exception:
                summary = ""
        if item.external_app:
            # External app: dim path; no trailing arrow (leading ↗ on label).
            return [(summary, "dim")] if summary else []
        # Internal drill — same as KIND_SUBMENU.
        if summary:
            return [(summary + " ", "dim"), (_GLYPH_DRILL, "bold cyan")]
        return [(_GLYPH_DRILL, "bold cyan")]

    if item.kind == KIND_PICKER and item.picker_getter is not None:
        try:
            v = item.picker_getter(app)
        except Exception:
            v = None
        if isinstance(v, list):
            value_str = f"{len(v)} selected" if v else "(none)"
        else:
            value_str = str(v) if v not in (None, "") else "(unset)"
        return [(value_str + " ", "bold"), (_GLYPH_PICKER, "bold cyan")]

    if item.kind in (KIND_SCALAR, KIND_DISPLAY):
        v = ""
        if item.value_getter is not None:
            try:
                v = item.value_getter(app) or ""
            except Exception:
                v = ""
        return [(v, "bold")] if v else []

    return []


def _render_header(item: MenuItem, width: int | None) -> Text:
    """Group sub-header rendered as ``─ Label ─────────``.

    Accent colour throughout (rule + label). The rule fills the row to
    the same right edge content rows reach so the buffer between text
    and the bordered subsection's right edge stays consistent."""
    label_part = f" {item.label} "
    if width is not None:
        # `used` already includes the leading ─; tail should just fill
        # whatever budget remains. The previous `- 1` over-subtracted
        # and left a visibly wider buffer on header rows than content
        # rows inside the bordered subsections.
        used = len(label_part) + 1
        tail = max(2, width - used)
    else:
        tail = 30
    text = Text()
    text.append("─", style="bold cyan")
    text.append(label_part, style="bold cyan")
    text.append("─" * tail, style="cyan")
    return text


# ── Bottom edit bar ──────────────────────────────────────────────────


class EditBar(Horizontal):
    """One-line scalar editor that mounts above the hint bar.

    Public state via ``open(item)`` / ``close()``. Posts an
    :class:`EditCommitted` message when the user submits a valid value;
    the parent screen updates the row display and closes the bar.
    """

    DEFAULT_CSS = """
    EditBar {
        dock: bottom;
        height: 2;
        padding: 0 1;
        margin-bottom: 1;
        background: $surface;
    }
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
        # Path-validation debounce: holds the pending Timer so a rapid
        # keystroke can cancel its predecessor before the iterdir runs.
        self._validation_timer: Any = None

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

    # Rows that participate in live path validation. Any KIND_SCALAR row
    # whose id is in this set surfaces ✓/✗ feedback in the error label as
    # the user types. Adding a new path-typed field is a one-line change.
    _PATH_VALIDATE_IDS: ClassVar[frozenset[str]] = frozenset({"wiz.path", "form.path"})

    # Cap the directory walk so a path like ~/Downloads doesn't stall the
    # UI on every keystroke. 5_000 is enough to communicate "lots" without
    # paying the cost of counting them all.
    _PATH_ENTRY_CAP: ClassVar[int] = 5_000

    # Delay before a path-validation keystroke runs the iterdir+stat
    # probe. 250 ms is short enough that a user who pauses to read still
    # sees feedback, but long enough that a continuous typing burst
    # never pays the disk cost.
    _PATH_VALIDATE_DEBOUNCE_S: ClassVar[float] = 0.25

    @on(Input.Changed, "#editor_input")
    def _on_input_changed(self, ev: Input.Changed) -> None:
        """For path-typed scalar rows (Add Collection wizard, per-source
        form), schedule a debounced ✓/✗ probe so we don't ``iterdir`` on
        every keystroke into a large directory like ~/Documents."""
        if self._item is None or self._item.id not in self._PATH_VALIDATE_IDS:
            return
        if self._validation_timer is not None:
            import contextlib

            with contextlib.suppress(Exception):
                self._validation_timer.stop()
            self._validation_timer = None
        raw_value = ev.value
        self._validation_timer = self.set_timer(
            self._PATH_VALIDATE_DEBOUNCE_S,
            lambda r=raw_value: self._validate_path(r),
        )

    def _validate_path(self, value: str) -> None:
        """Run the actual ✓/✗ probe (called by the debounce timer)."""
        self._validation_timer = None
        from pathlib import Path as _Path

        raw = value.strip().strip("'\"")
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
            n = 0
            for _ in p.iterdir():
                n += 1
                if n >= self._PATH_ENTRY_CAP:
                    self._set_status(f"✓ {self._PATH_ENTRY_CAP}+ entries", tone="ok")
                    return
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
    SettingsList > VerticalScroll { padding: 0 0; }
    SettingsList Static.row { height: 1; padding: 0 1; }
    SettingsList Static.row.-header-1 { padding: 1 1 0 1; height: 2; }
    SettingsList Static.row.-header-2 { padding: 0 1; }
    SettingsList .subsection {
        border: round $primary 50%;
        padding: 0 1;
        margin: 1 0 0 0;
        height: auto;
    }
    SettingsList .subsection:focus-within { border: round $accent; }
    /* Context-relevant section (Keybindings cheat sheet only today):
       header gets an accent border-left + bold; body rows get a faint
       tint so the eye lands on the section the user came from. */
    SettingsList Static.row.-hint-section.-header-2 {
        color: $accent;
        text-style: bold;
        border-left: thick $accent;
    }
    SettingsList Static.row.-hint-section { background: $accent 8%; }
    /* Only paint the cursor row when the list itself owns focus. When
       the screen's filter Input is focused, the user is composing a
       query — a list-cursor highlight at the same time would compete
       for the eye. */
    SettingsList:focus Static.row.-cursor { background: $accent 40%; text-style: bold; }
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
        # The active search query, lowercased — used to bold the matching
        # substring inside each filtered row's label.
        self._search_query: str = ""
        # When set, the next ``_init_cursor`` lands on this item id instead
        # of the first selectable row — lets a re-population (e.g. resume
        # from a popped child screen) keep the cursor on the row the user
        # drilled from. Consumed (reset to None) once applied.
        self._pending_cursor_id: str | None = None
        # Width the rows were last rendered at. Rows are width-dependent
        # (ellipsis / wrap), so only a width change needs a full rebuild;
        # height-only or duplicate resizes are skipped. -1 = never rendered.
        self._last_render_width: int = -1

    def compose(self) -> ComposeResult:
        # VerticalScroll (not plain Vertical) so long lists like the
        # Keybindings cheat sheet — 30+ rows after the registry-derived
        # rebuild — get a working scrollbar and ``_scroll_cursor_into_view``
        # has a scrollable parent to call ``scroll_to_widget`` on.
        yield VerticalScroll(id="settings_list_body")

    # ── Population ──────────────────────────────────────────────

    def set_items(
        self,
        items: list[MenuItem],
        breadcrumbs: dict[int, tuple[str, ...]] | None = None,
        cursor_id: str | None = None,
    ) -> None:
        self._items = list(items)
        self._search_breadcrumbs = dict(breadcrumbs) if breadcrumbs else {}
        # _init_cursor (deferred below) lands here if the id is present.
        self._pending_cursor_id = cursor_id
        body = self.query_one("#settings_list_body", VerticalScroll)
        # Remove existing rows synchronously by walking the DOM directly —
        # Textual's `remove_children` is async and would race against the
        # mount of fresh rows immediately below.
        for child in list(body.children):
            child.remove()
        # Track whether the current header was marked as the
        # "context-hint" section by its provider (sentinel in keywords);
        # if so, every body row until the next header gets the same
        # ``-hint-section`` class so the whole band paints together.
        in_hint_section = False
        # Group contiguous items that share a non-None `subsection` into
        # a bordered Vertical with the subsection name as border_title.
        # Items with subsection=None mount at the top level (the existing
        # flat-list behaviour). Suppressed in cross-tree search results
        # (where ``breadcrumbs`` is populated) since the per-row
        # breadcrumb already carries the section context and subsection
        # borders would fragment the result list.
        rendering_search = bool(self._search_breadcrumbs)
        current_subsection: str | None = None
        current_container: Vertical | VerticalScroll = body
        for item in items:
            target_sub = None if rendering_search else item.subsection
            if target_sub != current_subsection:
                # Close previous bordered group, open a new one if needed.
                if target_sub is None:
                    current_container = body
                else:
                    sub = Vertical(classes="subsection")
                    sub.border_title = target_sub
                    body.mount(sub)
                    current_container = sub
                current_subsection = target_sub
            cls = "row"
            if item.kind == KIND_HEADER:
                cls += f" -header-{item.header_level or 1}"
                in_hint_section = "_hint_section_" in (item.keywords or ())
                if in_hint_section:
                    cls += " -hint-section"
            elif in_hint_section:
                cls += " -hint-section"
            current_container.mount(Static("", classes=cls))
        self.call_after_refresh(self._init_cursor)

    def _init_cursor(self) -> None:
        target: int | None = None
        if self._pending_cursor_id is not None:
            target = next(
                (
                    i
                    for i, it in enumerate(self._items)
                    if it.id == self._pending_cursor_id and it.kind != KIND_HEADER
                ),
                None,
            )
            self._pending_cursor_id = None
        if target is None:
            target = self._first_selectable(0, +1)
        self.cursor_index = target if target is not None else 0
        self._render_all()
        self._post_highlight()

    # ── Render ──────────────────────────────────────────────────

    def _render_all(self) -> None:
        app: FNDApp = self.app  # type: ignore[assignment]
        try:
            body = self.query_one("#settings_list_body", VerticalScroll)
        except Exception:
            return
        width = self.size.width or 80
        rows = list(body.query(Static))
        highlight = self._search_query or None
        # Budget chars eaten by the wrapping containers so the row's
        # ellipsis fires before content clips past a border. The outer
        # box contributes border (2) + padding (2); a bordered
        # subsection adds border (2) + padding (2) plus a fudge factor
        # for the bullet column + cursor glyph that ride on the inside
        # of every row inside a subsection.
        outer_inset = 4
        subsection_inset = 6
        for i, (item, row) in enumerate(zip(self._items, rows, strict=False)):
            bc = self._search_breadcrumbs.get(id(item)) or None
            inset = outer_inset + (subsection_inset if item.subsection else 0)
            row.update(
                _render_row(
                    item,
                    app,
                    width=width - inset,
                    breadcrumb=bc,
                    highlight=highlight,
                )
            )
            if i == self.cursor_index and item.kind != KIND_HEADER:
                row.add_class("-cursor")
            else:
                row.remove_class("-cursor")

    def on_resize(self, ev: events.Resize) -> None:
        # Only a width change affects row rendering; skip height-only or
        # duplicate resizes so they don't trigger a stray full rebuild
        # (e.g. a late layout resize landing mid cursor-navigation).
        if ev.size.width == self._last_render_width:
            return
        self._last_render_width = ev.size.width
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

    def watch_cursor_index(self, old: int, new: int) -> None:
        """Move the ``-cursor`` class from the previously-cursored row
        to the newly-cursored row without re-rendering every row.

        ``_render_all`` rebuilds every row's Rich ``Text`` and updates
        every ``Static`` — on a long list (Keybindings has ~80 rows) it
        dominates the cost of a single arrow keystroke. The cursor move
        only changes one CSS class on two rows; do exactly that.

        Also posts the Highlighted message so the parent screen can
        update its hint bar / detail strip — any external setter of
        ``cursor_index`` (screen restoration, jump-to-row, tests) goes
        through this watcher so the cascade always fires.
        """
        try:
            body = self.query_one("#settings_list_body", VerticalScroll)
        except Exception:
            return
        rows = list(body.query(Static))
        if 0 <= old < len(rows):
            rows[old].remove_class("-cursor")
        if 0 <= new < len(rows) and new < len(self._items) and self._items[new].kind != KIND_HEADER:
            rows[new].add_class("-cursor")
        # Notify the parent screen so hint bar + detail strip refresh.
        self._post_highlight()

    def _post_highlight(self) -> None:
        if 0 <= self.cursor_index < len(self._items):
            self.post_message(self.Highlighted(self._items[self.cursor_index]))
        else:
            self.post_message(self.Highlighted(None))

    def action_move(self, delta: int) -> None:
        n = len(self._items)
        if n == 0:
            return
        # At the topmost selectable row + Up → hand focus to the
        # screen's filter Input so arrow keys bridge the boundary
        # both ways (the Input has its own Down handler that
        # bridges back into the list).
        if delta == -1:
            top = self._first_selectable(0, +1)
            if top is not None and self.cursor_index == top:
                import contextlib

                with contextlib.suppress(Exception):
                    self.screen.query_one("#settings_search", Input).focus()
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
            body = self.query_one("#settings_list_body", VerticalScroll)
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
        from fnd import opener

        opener.reveal(path)

    def _reveal_target(self, item: MenuItem) -> Path | None:
        """Return the file path to reveal for ``item``, or None if the row
        isn't reveal-capable."""
        from fnd.config import default_config_path

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
        # Down from the filter Input → focus the SettingsList. Screen-
        # level binding works because SettingsList's own Down binding
        # consumes the key when the list has focus, so this only
        # fires while the Input is focused (Input has no Down handler).
        Binding("down", "list_from_input", show=False),
    ]

    CSS = """
    SettingsScreen { background: $surface; align: center middle; }
    SettingsScreen > #settings_box {
        height: auto;
        max-height: 90%;
        width: 75%;
        min-width: 60;
        max-width: 140;
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
    SettingsScreen #settings_status {
        height: 1; padding: 0 1; color: $text-muted;
    }
    """

    def __init__(
        self,
        *,
        breadcrumb: tuple[str, ...],
        items: tuple[MenuItem, ...],
        provider: Callable[[FNDApp], Iterable[MenuItem]] | None = None,
    ) -> None:
        super().__init__()
        self._breadcrumb = breadcrumb
        self._items: tuple[MenuItem, ...] = tuple(items)
        # Re-invoked on screen resume so structural edits in a popped
        # child screen (add/remove source, rename, etc.) appear here
        # immediately. ``None`` falls back to a value-only re-render.
        self._provider = provider
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
            if not self._breadcrumb:
                # Root-only version + build identifier; sub-screens omit it.
                yield Static("", id="settings_status")
        yield EditBar()
        yield Static("", id="footer_hints")

    def on_mount(self) -> None:
        lst = self.query_one(SettingsList)
        lst.set_items(list(self._items))
        # Root menu: focus the filter Input so typing immediately
        # narrows. Sub-menus focus the list — the user just drilled
        # in deliberately and wants to navigate, not re-filter from
        # scratch. The `/` shortcut still works to focus the filter
        # on demand from any screen.
        if not self._breadcrumb:
            self.query_one("#settings_search", Input).focus()
        else:
            lst.focus()
        self._render_footer()
        self._seed_detail_strip()
        if not self._breadcrumb:
            self._render_version_status()

    def on_screen_resume(self) -> None:
        """Refresh items when control returns from a popped child screen.

        A scalar/picker edit only changes one row's value — re-rendering
        in place is enough since the row's ``value_getter`` lambda reads
        config lazily. But a structural edit (adding a source, renaming
        a collection) changes the *set* of rows, and the cached
        ``self._items`` list won't reflect it. Re-running the provider
        is the single source of truth: if it returns a different shape,
        the new rows appear; if it returns the same shape, only the
        trailing values needed refreshing.

        Also invalidates the lazy-trailing cache so async values (cache
        size, pdf-structure disk, etc.) re-compute on resume — the user
        may have just run an action that changed the underlying numbers.
        """
        import contextlib

        from fnd.tui.lazy_trailing import invalidate

        for key in (
            "indexing.cache_size",
            "indexing.pdf_status",
            # The cache-size chip's real key (the old "indexing.summary.
            # cache_short" was renamed but left dead here, so the chip
            # showed a stale size after cache actions).
            "pdf_texture.summary.cache_short",
            "cache.stale_count",
            "cache.retexturise_outdated",
            "pdf_texture.summary.stale_short",
        ):
            invalidate(key)

        if self._provider is None:
            with contextlib.suppress(Exception):
                self.query_one(SettingsList).refresh_values()
            self._refresh_hint_bar()
            return
        try:
            new_items = tuple(self._provider(self.app))  # type: ignore[arg-type]
        except Exception:
            with contextlib.suppress(Exception):
                self.query_one(SettingsList).refresh_values()
            self._refresh_hint_bar()
            return
        try:
            lst = self.query_one(SettingsList)
        except Exception:
            self._refresh_hint_bar()
            return
        # Preserve cursor position by item id: a structural edit (add /
        # remove row) shifts indices, so the closest semantic anchor is
        # the previously-cursored row's stable id. Threaded through
        # set_items → _init_cursor so the restore is atomic — a separate
        # deferred restore loses the race against _init_cursor's own
        # deferred reset-to-first and the cursor jumps back to the top.
        prev_id: str | None = None
        if 0 <= lst.cursor_index < len(lst._items):
            prev_id = lst._items[lst.cursor_index].id
        self._items = new_items
        lst.set_items(list(new_items), cursor_id=prev_id)
        self._refresh_hint_bar()

    def _render_version_status(self) -> None:
        """Show `fnd vX.Y.Z` at the bottom of the root menu so users
        can spot the version without leaving the TUI."""
        from fnd import __version__

        self.query_one("#settings_status", Static).update(f"fnd v{__version__}")

    def _seed_detail_strip(self) -> None:
        """Populate the detail strip with the first selectable item so
        the user sees content immediately on open."""
        first = next((it for it in self._items if it.is_selectable), None)
        if first is not None:
            strip = self.query_one(DetailStrip)
            strip.set(
                first.description or "",
                self._row_metadata(first),
                markup=first.description_markup,
            )

    # ── Footer ──────────────────────────────────────────────────

    def _render_footer(self) -> None:
        """Pick the hint-bar cluster based on focus, edit-bar state,
        breadcrumb, and cursor row."""
        app: FNDApp = self.app  # type: ignore[assignment]
        cluster = self._hint_cluster()
        self.query_one("#footer_hints", Static).update(_hint_bar(app, cluster))

    def _hint_cluster(self) -> tuple[tuple[str, str], ...]:
        """Choose the contextual hint cluster for the current state.

        Priority: edit-bar open > search input focused > Keybindings
        sub-screen > cursor-row-kind-aware default.

        For the default branch, the `⏎` action label reflects what
        Enter does on the focused row (Toggle / Edit / Choose / Open /
        Run / Open in editor) — or is omitted entirely for read-only
        rows. This way the footer never lies about the next action.
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

        # Default — per-kind ⏎ label. Reveal append on external-app rows.
        cursor_item = self._cursor_item()
        nav = ("↑↓", "Nav")
        back = ("←", "Back")
        filt = ("/", "Filter")

        if cursor_item is None:
            return (nav, ("⏎", "Open"), back, filt)

        kind = cursor_item.kind
        if kind == KIND_DISPLAY:
            # Read-only: no ⏎ entry. The dim label + absent affordance
            # tell the user Enter does nothing.
            return (nav, back, filt)
        if kind == KIND_TOGGLE:
            return (nav, ("⏎", "Toggle"), back, filt)
        if kind == KIND_SCALAR:
            return (nav, ("⏎", "Edit"), back, filt)
        if kind == KIND_PICKER:
            return (nav, ("⏎", "Choose"), back, filt)
        if kind == KIND_ACTION:
            return (nav, ("⏎", "Run"), back, filt)
        if kind == KIND_EXTERNAL and cursor_item.external_app:
            return (nav, ("⏎", "Open in editor"), ("Shift+⏎", "Reveal"), back)
        # KIND_SUBMENU and drill KIND_EXTERNAL: "Open" (push a screen).
        return (nav, ("⏎", "Open"), back, filt)

    def _cursor_item(self) -> MenuItem | None:
        """Return the MenuItem the cursor is on, or None if not available."""
        try:
            lst = self.query_one(SettingsList)
        except Exception:
            return None
        if 0 <= lst.cursor_index < len(lst._items):
            return lst._items[lst.cursor_index]
        return None

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
        lst._search_query = q
        if not q:
            self._filter_active = False
            self._search_breadcrumbs = {}
            lst.set_items(list(self._items))
            return
        self._filter_active = True
        filtered, breadcrumbs = self._filter_items(q)
        self._search_breadcrumbs = breadcrumbs
        if not filtered:
            # Empty-state hint — a non-selectable placeholder row so the
            # cursor-skip rule keeps it inert.
            placeholder = MenuItem(
                id="search.empty",
                label=(
                    f"No matches for '{ev.value.strip()}'. Try shorter terms or press Esc to clear."
                ),
                kind=KIND_HEADER,
            )
            lst.set_items([placeholder])
            return
        lst.set_items(filtered, breadcrumbs=breadcrumbs)

    def _filter_items(self, q: str) -> tuple[list[MenuItem], dict[int, tuple[str, ...]]]:
        """Cross-section: walk every section's leaves, score by substring
        match against label + key + keywords + breadcrumb segments.

        Spec deliberately excludes ``description`` prose — descriptions
        surface in the detail strip on focus, and indexing them muddies
        the search results.
        """
        matches: list[tuple[int, MenuItem, tuple[str, ...]]] = []
        app: FNDApp = self.app  # type: ignore[assignment]
        for path, item in walk_all_sections(app):
            if item.kind == KIND_HEADER:
                continue
            haystack = " ".join((item.label, item.key, *item.keywords, *path)).lower()
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
        # Search is navigation-only: Enter lands focus on the first match
        # (the cursor is already there from filtering) and stops. It does
        # NOT fire the row's effect — no silent toggles, no accidental
        # side-effects (e.g. launching an editor). The user presses Enter
        # again on the now-focused list to actually act on the row.
        lst = self.query_one(SettingsList)
        lst.focus()
        lst._post_highlight()

    def action_list_from_input(self) -> None:
        """Bridge Down from the filter Input into the list. Up at the
        topmost row bridges back (see :meth:`SettingsList.action_move`).
        Left/Right stay as text-cursor movement inside the Input
        (Textual default), so the Input still feels like a normal text
        field."""
        self.query_one(SettingsList).focus()

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
            strip.set(
                item.description or "",
                self._row_metadata(item),
                markup=item.description_markup,
            )
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
        app: FNDApp = self.app  # type: ignore[assignment]
        if item.kind == KIND_DISPLAY:
            # Read-only row — Enter does nothing. Detail strip still
            # populates from cursor focus.
            return
        if item.kind == KIND_ACTION:
            # ACTION rows have three dispatch paths in priority order:
            #   1. ``external`` callable (custom side-effect, e.g. push
            #      a confirm modal or an IndexerScreen). When present,
            #      run it and leave the settings stack alone — the
            #      callable usually pushes its own screen.
            #   2. ``action_id`` (REGISTRY-bound app action). Close the
            #      settings stack and dispatch via ``app.action_<id>``.
            #   3. Documentation-only rows (widget-level bindings — Move
            #      cursor, Activate, etc. in the Keybindings sheet)
            #      carry neither; Enter is a no-op so the user can keep
            #      reading the cheat sheet.
            if item.external is not None:
                item.external(app)
                return
            if not item.action_id:
                return
            self._close_settings_stack()
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
            self.app.push_screen(
                TreePickerScreen(item) if item.groups_provider is not None else PickerScreen(item)
            )
            return
        if item.kind == KIND_EXTERNAL:
            if item.external is not None:
                item.external(app)
            return
        if item.kind == KIND_SUBMENU:
            children = item.resolve_children(app)

            def _provider(a: FNDApp, _it: MenuItem = item) -> tuple[MenuItem, ...]:
                return tuple(_it.resolve_children(a))

            self.app.push_screen(
                SettingsScreen(
                    breadcrumb=(*self._breadcrumb, item.label),
                    items=children,
                    provider=_provider,
                )
            )

    @on(EditBar.EditCommitted)
    def _on_edit_committed(self, ev: EditBar.EditCommitted) -> None:
        app: FNDApp = self.app  # type: ignore[assignment]
        item = ev.item
        try:
            if item.setting_path:
                from fnd.config import default_config_path, load, write_setting

                write_setting(
                    config_path=default_config_path(),
                    dotted_path=item.setting_path,
                    value=ev.value,
                )
                app._config = load()  # type: ignore[attr-defined]
                app._search.ranking_profile = app._search.resolve_profile()  # type: ignore[attr-defined]
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
        app: FNDApp = self.app  # type: ignore[assignment]
        hints: tuple[tuple[str, str], ...] = (
            (("⏎", "Toggle"), ("Esc", "Save"))
            if self._item.multi
            else (("⏎", "Select"), ("Esc", "Cancel"))
        )
        self.query_one("#footer_hints", Static).update(_hint_bar(app, hints))

    def _render_options(self) -> None:
        """First-paint of the picker list. Toggles after mount use
        ``replace_option_prompt_at_index`` so the cursor is preserved."""
        lst = self.query_one("#picker_list", OptionList)
        lst.clear_options()
        if not self._choices:
            lst.add_option(Option(Text("(no options)", style="dim"), disabled=True))
            return
        for c in self._choices:
            lst.add_option(Option(self._render_choice_prompt(c), id=str(c.value)))

    @on(OptionList.OptionSelected, "#picker_list")
    def _on_selected(self, ev: OptionList.OptionSelected) -> None:
        if ev.option.id is None:
            return
        target_index = next(
            (i for i, c in enumerate(self._choices) if str(c.value) == ev.option.id),
            None,
        )
        if target_index is None:
            return
        target = self._choices[target_index]
        if self._item.multi:
            if target.value in self._selected:
                self._selected.discard(target.value)
            else:
                self._selected.add(target.value)
            # In-place update keeps the OptionList cursor on the toggled
            # row — rebuilding via clear_options + add_option would reset
            # it to index 0 every time and force the user to re-navigate.
            lst = self.query_one("#picker_list", OptionList)
            lst.replace_option_prompt_at_index(target_index, self._render_choice_prompt(target))
            return
        self._commit({target.value})
        self.app.pop_screen()

    def _render_choice_prompt(self, c: ChoiceOption) -> Text:
        marker = "✓" if c.value in self._selected else " "
        t = Text(f"[{marker}] {c.label}")
        if c.description:
            t.append(f"   {c.description}", style="dim")
        return t

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


def _includes_groups() -> list[ToggleGroup]:
    """Category → kind model for the Includes nested picker (all registry
    kinds, since a source can index any supported type)."""
    from fnd.kinds import CATEGORIES, KIND_BY_ID, KINDS_IN_CATEGORY

    groups: list[ToggleGroup] = []
    for cat in CATEGORIES:
        items = tuple(
            ToggleItem(k, f"{KIND_BY_ID[k].label} ({'/'.join(KIND_BY_ID[k].suffixes)})")
            for k in KINDS_IN_CATEGORY[cat.id]
        )
        if items:
            groups.append(ToggleGroup(cat.id, cat.label, items))
    return groups


class TreePickerScreen(Screen[None]):
    """Nested category→item multi-select for a picker item that supplies a
    ``groups_provider``. Reuses the shared :class:`ToggleTree`, so it toggles,
    cascades, and repaints exactly like the file-type filter. Esc commits."""

    BINDINGS = [  # noqa: RUF012
        Binding("escape", "back", "Back", show=False),
    ]

    CSS = """
    TreePickerScreen { background: $surface; }
    TreePickerScreen > #settings_box {
        height: 1fr; border: round $primary 50%; padding: 0 1;
    }
    TreePickerScreen > #settings_box:focus-within { border: round $accent; }
    TreePickerScreen > #footer_hints {
        dock: bottom; height: 1; background: $surface; padding: 0 1; color: $text-muted;
    }
    """

    def __init__(self, item: MenuItem) -> None:
        super().__init__()
        self._item = item

    def compose(self) -> ComposeResult:
        with Vertical(id="settings_box") as box:
            box.border_title = self._item.label
            yield ToggleTree(id="tree_picker")
        yield Static("", id="footer_hints")

    def on_mount(self) -> None:
        app: FNDApp = self.app  # type: ignore[assignment]
        groups = list(self._item.groups_provider(app)) if self._item.groups_provider else []
        current = self._item.picker_getter(app) if self._item.picker_getter else []
        selected = set(current) if isinstance(current, list | tuple | set) else set()
        tree = self.query_one("#tree_picker", ToggleTree)
        tree.set_model(groups, selected, expanded={g.id for g in groups})
        tree.focus()
        self.query_one("#footer_hints", Static).update(
            _hint_bar(app, (("⏎/Space", "Toggle"), ("←/→", "Collapse/Expand"), ("Esc", "Save")))
        )

    @on(ToggleTree.SelectionChanged)
    def _on_changed(self, ev: ToggleTree.SelectionChanged) -> None:
        # Commit live so the row summary updates as the user toggles.
        self._commit(ev.selected)

    def action_back(self) -> None:
        self._commit(self.query_one("#tree_picker", ToggleTree).selected)
        self.app.pop_screen()

    def _commit(self, values: frozenset[str]) -> None:
        if self._item.picker_setter is None:
            return
        try:
            self._item.picker_setter(self.app, sorted(values))  # type: ignore[arg-type]
        except Exception as e:
            self.notify(_summarize(e), severity="error", title="Save failed")


# ── Collection-form screens (rebuilt from CollectionsScreen) ────────


def _includes_choices() -> list[ChoiceOption]:
    """Includes multi-select options: every registry kind, labelled with its
    category (so the flat list reads grouped), plus a custom-glob sentinel."""
    from fnd.kinds import CATEGORY_BY_ID, KIND_SPECS

    out = [
        ChoiceOption(
            value=spec.id,
            label=f"{CATEGORY_BY_ID[spec.category].label} · {spec.label} ({'/'.join(spec.suffixes)})",
        )
        for spec in KIND_SPECS
    ]
    out.append(
        ChoiceOption(
            value="__custom__",
            label="Custom glob…",
            description="Add a free-form glob pattern (e.g. `**/*.org`).",
        )
    )
    return out


def _kinds_to_include_globs(kind_ids: list[str]) -> list[str]:
    """Expand selected kind ids to include globs for all their suffixes."""
    from fnd.kinds import KIND_BY_ID

    globs: list[str] = []
    for kid in kind_ids:
        spec = KIND_BY_ID.get(kid)
        if spec is not None:
            globs.extend(f"**/*{sfx}" for sfx in spec.suffixes)
    return globs


def _split_includes_globs(globs: list[str]) -> tuple[list[str], str]:
    """Map includes globs back to ``(kind_ids, custom_blob)``.

    A kind is recognised as selected iff any of its suffix globs (``**/*<sfx>``)
    is present; those globs are then consumed. Whatever remains becomes the
    comma-joined custom blob so the user keeps their original patterns verbatim.
    """
    from fnd.kinds import KIND_SPECS

    remaining = list(globs)
    kinds: list[str] = []
    for spec in KIND_SPECS:
        kglobs = [f"**/*{sfx}" for sfx in spec.suffixes]
        if any(g in remaining for g in kglobs):
            kinds.append(spec.id)
            remaining = [g for g in remaining if g not in kglobs]
    return kinds, ", ".join(remaining)


def _split_excludes_globs(globs: list[str]) -> tuple[list[str], str]:
    """Map excludes globs back to ``(preset_keys, custom_blob)``.

    A preset is considered selected iff every glob it ships is present.
    Once a preset's globs are consumed they are removed from the remaining
    pool; whatever's left becomes the comma-joined custom blob.
    """
    from fnd.config import EXCLUDES_PRESETS

    remaining = list(globs)
    preset_keys: list[str] = []
    for key, preset in EXCLUDES_PRESETS.items():
        preset_globs = preset["globs"]
        if all(g in remaining for g in preset_globs):
            preset_keys.append(key)
            for g in preset_globs:
                remaining.remove(g)
    return preset_keys, ", ".join(remaining)


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
        Binding("ctrl+a", "save_add_another", show=False),
        Binding("ctrl+d", "delete_source", "Delete", show=False),
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
    SourceFormScreen #form_error { color: $error; padding: 0 1; height: auto; }
    SourceFormScreen #form_error.-hidden { display: none; }
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
            "includes": [],  # list[str] of indexer ext keys (md / pdf / …)
            "includes_custom": "",  # comma-separated free-form globs
            "excludes_presets": [],  # list[str] of EXCLUDES_PRESETS keys
            "excludes_custom": "",  # comma-separated free-form globs
            "filter": "",
            "follow_symlinks": False,
            # Phase 2b: per-source app override + Obsidian vault.
            # ``app`` is the registry id (or "" = no override → resolver
            # walks the global app_defaults / auto-promote ladder).
            # ``app_params_vault`` is the only app_param that has UI
            # surface today; other params still reachable via the TOML.
            "app": "",
            "app_params_vault": "",
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
            yield Static("", id="form_error", classes="-hidden")
        yield EditBar()
        yield Static("", id="footer_hints")

    def _show_error(self, message: str) -> None:
        err = self.query_one("#form_error", Static)
        err.update(message)
        err.remove_class("-hidden")

    def _clear_error(self) -> None:
        err = self.query_one("#form_error", Static)
        err.update("")
        err.add_class("-hidden")

    # ── Lifecycle ──────────────────────────────────────────────

    def on_mount(self) -> None:
        self._load_snapshot()
        self._populate_fields()
        self._render_footer()
        self.query_one(SettingsList).focus()

    def _load_snapshot(self) -> None:
        app: FNDApp = self.app  # type: ignore[assignment]
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
        exts, includes_custom = _split_includes_globs(list(s.includes))
        preset_keys, excludes_custom = _split_excludes_globs(list(s.excludes))
        self._fields = {
            "path": str(s.path),
            "includes": exts,
            "includes_custom": includes_custom,
            "excludes_presets": preset_keys,
            "excludes_custom": excludes_custom,
            "filter": s.frontmatter_filter or "",
            "follow_symlinks": bool(s.follow_symlinks),
            "app": s.app or "",
            "app_params_vault": (s.app_params or {}).get("vault", ""),
        }
        self._snapshot = {
            "path": self._fields["path"],
            "includes": list(self._fields["includes"]),
            "includes_custom": self._fields["includes_custom"],
            "excludes_presets": list(self._fields["excludes_presets"]),
            "excludes_custom": self._fields["excludes_custom"],
            "filter": self._fields["filter"],
            "follow_symlinks": self._fields["follow_symlinks"],
            "app": self._fields["app"],
            "app_params_vault": self._fields["app_params_vault"],
        }

    def _populate_fields(self) -> None:
        self.query_one(SettingsList).set_items(self._build_field_items())

    def _build_field_items(self) -> list[MenuItem]:
        from fnd.config import EXCLUDES_PRESETS

        return [
            self._field_item("path", "Path", hint="path or ~/path"),
            MenuItem(
                id="form.includes",
                label="Includes",
                kind=KIND_PICKER,
                multi=True,
                choices_provider=lambda _app: _includes_choices(),
                groups_provider=lambda _app: _includes_groups(),
                picker_getter=lambda _app: self._includes_picker_state(),
                picker_setter=lambda _app, vs: self._set_includes(vs),
            ),
            MenuItem(
                id="form.excludes",
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
                picker_setter=lambda _app, vs: self._set_excludes(vs),
            ),
            self._field_item("filter", "Filter", hint="frontmatter DSL"),
            MenuItem(
                id="form.follow_symlinks",
                label="Follow symlinks",
                kind=KIND_TOGGLE,
                toggle_getter=lambda _app: bool(self._fields["follow_symlinks"]),
                toggle_setter=lambda _app, v: self._set_follow(v),
            ),
            MenuItem(
                id="form.app",
                label="App",
                description=(
                    "Open files from this source with a specific app. "
                    "Leave as '(default)' to use the global app_defaults "
                    "+ auto-promote ladder. See ``[apps]`` in config.toml "
                    "and docs/apps/ for the full list."
                ),
                kind=KIND_PICKER,
                multi=False,
                choices_provider=self._app_choices,
                picker_getter=lambda _app: self._fields.get("app") or "",
                picker_setter=lambda _app, v: self._set_app(v),
            ),
            self._field_item(
                "app_params_vault",
                "Obsidian vault",
                description=(
                    "Add the Advanced URI plugin to your vault for "
                    "line-precise jumps; without it, lands at section."
                ),
                hint="Vault name (auto-detected when App = Obsidian)",
            ),
        ]

    def _app_choices(self, _app: Any) -> list[ChoiceOption]:
        """All registered apps + a '(default)' sentinel for clearing the
        per-source override. Built-ins and user apps from [apps.<id>]
        appear together."""
        from fnd.apps import build_registry

        cfg_obj = self.app._config  # type: ignore[attr-defined]
        registry = build_registry(cfg_obj)
        out: list[ChoiceOption] = [
            ChoiceOption(
                value="",
                label="(default: use global resolver)",
                description="No per-source override; defer to app_defaults + auto-promote.",
            )
        ]
        for app_id, app in registry.items():
            # Filter to apps that are actually installed on this host —
            # matches the global default-app picker and the Open-with
            # modal, both of which already filter by ``available()``.
            # Without this, the picker would let users pick e.g. Skim on
            # a machine that doesn't have Skim and only fail at open
            # time. ``system`` is always available so it stays.
            if not app.available():
                continue
            handles = ",".join(app.handles)
            # ``app.notes`` carries the per-app advisory ("install plugin X
            # for line-precise jumps", "no page-jump on macOS", etc.) — surface
            # it as the picker's description so users see the recommendation
            # at the point of choice. Fall back to ``handles:`` when an app
            # has no notes (most built-ins do).
            desc = app.notes if app.notes else f"handles: {handles}"
            out.append(
                ChoiceOption(
                    value=app_id,
                    label=app.display_name,
                    description=desc,
                )
            )
        return out

    def _set_app(self, value: str) -> None:
        """Update the App field. When the user picks Obsidian and no
        vault is set yet, auto-detect from the source path."""
        self._fields["app"] = value or ""
        if value == "obsidian" and not str(self._fields.get("app_params_vault") or "").strip():
            from fnd.apps import detect_obsidian_vault

            path_s = str(self._fields.get("path") or "").strip()
            if path_s:
                try:
                    detected = detect_obsidian_vault(Path(path_s).expanduser())
                except (ValueError, OSError):
                    detected = None
                if detected:
                    self._fields["app_params_vault"] = detected
        self.query_one(SettingsList).refresh_values()

    def _includes_picker_state(self) -> list[str]:
        # Nested tree picker seed: current kinds, or ALL kinds when empty so a
        # new source opens with every type selected (empty includes = index all).
        from fnd.kinds import ALL_KIND_IDS

        inc = list(self._fields["includes"])
        return inc if inc else list(ALL_KIND_IDS)

    def _excludes_picker_state(self) -> list[str]:
        state = list(self._fields["excludes_presets"])
        if str(self._fields.get("excludes_custom") or "").strip():
            state.append("__custom__")
        return state

    def _set_includes(self, values: list[str]) -> None:
        # Tree picker commit: store the selected kind ids. All selected → store
        # empty (= index every supported type, and auto-pick up future types).
        # Any existing custom-glob value is preserved untouched.
        from fnd.kinds import ALL_KIND_IDS

        picked = [v for v in values if v in set(ALL_KIND_IDS)]
        self._fields["includes"] = [] if set(picked) >= set(ALL_KIND_IDS) else picked
        self.query_one(SettingsList).refresh_values()

    def _set_excludes(self, values: list[str]) -> None:
        picked = list(values)
        wants_custom = "__custom__" in picked
        self._fields["excludes_presets"] = [v for v in picked if v != "__custom__"]
        if wants_custom and not str(self._fields.get("excludes_custom") or "").strip():
            self._prompt_custom("excludes_custom", "Excludes custom globs (comma-separated)")
        elif not wants_custom:
            self._fields["excludes_custom"] = ""
        self.query_one(SettingsList).refresh_values()

    def _prompt_custom(self, field_key: str, label: str) -> None:
        item = MenuItem(
            id=f"form.{field_key}",
            label=label,
            kind=KIND_SCALAR,
            value_getter=lambda _app, key=field_key: str(self._fields.get(key) or ""),
        )
        self.query_one(EditBar).open(item, str(self._fields.get(field_key) or ""))

    def _field_item(self, key: str, label: str, *, hint: str, description: str = "") -> MenuItem:
        def _get(_app: Any) -> str:
            v = self._fields[key]
            if key == "filter" and v:
                status = self._parse_status(v)
                return f"{v}   {status}".rstrip()
            return v or "(unset)"

        return MenuItem(
            id=f"form.{key}",
            label=label,
            description=description,
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
        if item.kind == KIND_PICKER:
            self.app.push_screen(
                TreePickerScreen(item) if item.groups_provider is not None else PickerScreen(item)
            )
        elif item.kind == KIND_SCALAR:
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
                from fnd.filter_dsl import parse_or_error

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
        from fnd.filter_dsl import parse_or_error
        from fnd.frontmatter import FrontmatterParseError, read_frontmatter_from_text

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
        from fnd.filter_dsl import parse_or_error

        text = filter_text.strip()
        if not text:
            return ""
        _pred, err = parse_or_error(text)
        if err is None:
            return "✓"
        return f"✗ col {err.column}"

    # ── Footer ────────────────────────────────────────────────

    def _render_footer(self) -> None:
        app: FNDApp = self.app  # type: ignore[assignment]
        # Ctrl+D only meaningful when editing an existing source.
        hints: tuple[tuple[str, str], ...] = (
            ("Tab", "Fields ↔ sample"),
            ("⏎", "Edit"),
            ("Ctrl+S", "Save"),
            ("Esc", "Cancel"),
        )
        if self._source_index is not None:
            hints = (*hints, ("Ctrl+D", "Delete source"))
        self.query_one("#footer_hints", Static).update(_hint_bar(app, hints))

    # ── Save / cancel ────────────────────────────────────────

    def action_delete_source(self) -> None:
        """Push the delete-source confirmation modal. No-op when adding
        a new source (nothing to delete yet)."""
        if self._source_index is None:
            return
        self.app.push_screen(
            DeleteSourceScreen(
                collection_name=self._collection_name,
                source_index=self._source_index,
            )
        )

    def action_save_close(self) -> None:
        from pathlib import Path

        from fnd.config import (
            EXCLUDES_PRESETS,
            CollectionConfig,
            SourceConfig,
            default_config_path,
            load,
            write_collection,
        )

        self._clear_error()

        path = str(self._fields["path"] or "").strip().strip("'\"")
        if not path:
            self._show_error("Path is required.")
            return
        if not Path(path).expanduser().exists():
            self._show_error(f"Path does not exist: {path}")
            return
        # Reassemble globs from picker-driven fields.
        includes_globs: list[str] = _kinds_to_include_globs(list(self._fields["includes"]))
        for g in str(self._fields.get("includes_custom") or "").split(","):
            g = g.strip()
            if g:
                includes_globs.append(g)
        excludes_globs: list[str] = []
        for preset_id in self._fields["excludes_presets"]:
            excludes_globs.extend(EXCLUDES_PRESETS[preset_id]["globs"])
        for g in str(self._fields.get("excludes_custom") or "").split(","):
            g = g.strip()
            if g:
                excludes_globs.append(g)
        app_id = str(self._fields.get("app") or "").strip()
        vault = str(self._fields.get("app_params_vault") or "").strip()
        app_params: dict[str, str] = {"vault": vault} if vault else {}
        try:
            new_source = SourceConfig(
                path=Path(path),
                includes=includes_globs,
                excludes=excludes_globs,
                follow_symlinks=bool(self._fields["follow_symlinks"]),
                frontmatter_filter=(str(self._fields["filter"]) or None),
                app=app_id or None,
                app_params=app_params,
            )
        except Exception as e:
            self._show_error(_summarize(e))
            return

        app: FNDApp = self.app  # type: ignore[assignment]
        cfg = app._config  # type: ignore[attr-defined]
        if cfg is None or self._collection_name not in cfg.collections:
            self._show_error("Collection vanished. Please reopen the menu.")
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
            self._show_error(_summarize(e))
            return
        app._config = load()  # type: ignore[attr-defined]
        app._scope.refresh_collections_panel()  # type: ignore[attr-defined]
        # Trigger a reindex if the source set materially changed. Pop
        # FIRST so the IndexerScreen lands on top of the menu, not on
        # top of this wizard.
        needs_reindex = self._snapshot != self._fields or self._source_index is None
        self.app.pop_screen()
        if needs_reindex:
            app._indexer.reindex_with_warning(  # type: ignore[attr-defined]
                self._collection_name, rebuild=True
            )

    def action_save_add_another(self) -> None:
        """Save the current source, then immediately re-open the form
        for another new source in the same collection. Only meaningful
        when adding new (source_index is None); in edit-mode behaves
        like Ctrl+S."""
        collection = self._collection_name
        was_new = self._source_index is None
        self.action_save_close()
        if not was_new:
            return

        # action_save_close pops; push a fresh form once the pop has
        # settled so the user can continue adding without going back to
        # the SourcesScreen and re-triggering Add source.
        def _chain() -> None:
            self.app.push_screen(SourceFormScreen(collection_name=collection, source_index=None))

        self.app.call_later(_chain)

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
    AddCollectionWizard #wizard_error { color: $error; padding: 0 1; height: auto; }
    AddCollectionWizard #wizard_error.-hidden { display: none; }
    AddCollectionWizard > #footer_hints {
        dock: bottom; height: 1; background: $surface; padding: 0 1; color: $text-muted;
    }
    """

    def __init__(self) -> None:
        super().__init__()
        from fnd.config import EXCLUDES_PRESETS

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
            yield Static("", id="wizard_error", classes="-hidden")
            yield DetailStrip()
        yield EditBar()
        yield Static("", id="footer_hints")

    def _show_error(self, message: str) -> None:
        """Render an inline validation error in the wizard's #wizard_error
        Static. Phase 6 dropped the old `notify()` toast pattern so the
        user sees errors anchored to the form they're filling out."""
        err = self.query_one("#wizard_error", Static)
        err.update(message)
        err.remove_class("-hidden")

    def _clear_error(self) -> None:
        err = self.query_one("#wizard_error", Static)
        err.update("")
        err.add_class("-hidden")

    def on_mount(self) -> None:
        self._populate_fields()
        self.query_one(SettingsList).focus()
        app: FNDApp = self.app  # type: ignore[assignment]
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
        from fnd.config import EXCLUDES_PRESETS

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
                choices_provider=lambda _app: _includes_choices(),
                groups_provider=lambda _app: _includes_groups(),
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
                hint="frontmatter DSL",
                value_getter=lambda _app: self._filter_with_status(),
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
        n = len(self._fields["includes"])
        return "all types" if n == 0 else f"{n} type{'s' if n != 1 else ''}"

    def _summarize_excludes(self) -> str:
        return f"{len(self._fields['excludes_presets'])} presets"

    def _set_follow(self, value: bool) -> None:
        self._fields["follow_symlinks"] = bool(value)

    def _filter_with_status(self) -> str:
        """Trailing column for the Frontmatter filter row — shows the
        DSL string plus a live ``✓`` / ``✗ col N`` parse indicator so
        syntax mistakes surface without leaving the form."""
        text = str(self._fields.get("filter") or "").strip()
        if not text:
            return "(none)"
        from fnd.filter_dsl import parse_or_error

        _pred, err = parse_or_error(text)
        if err is None:
            return f"{text}   ✓"
        return f"{text}   ✗ col {err.column}"

    def _includes_picker_state(self) -> list[str]:
        """Nested tree picker seed: current kinds, or ALL kinds when empty so a
        new source opens with every type selected (empty includes = index all)."""
        from fnd.kinds import ALL_KIND_IDS

        inc = list(self._fields["includes"])
        return inc if inc else list(ALL_KIND_IDS)

    def _excludes_picker_state(self) -> list[str]:
        state = list(self._fields["excludes_presets"])
        if str(self._fields.get("excludes_custom") or "").strip():
            state.append("__custom__")
        return state

    def _set_includes(self, values: list[str]) -> None:
        """Tree picker commit: store selected kind ids. All selected → store
        empty (= index every supported type, future-proof). Preserves any
        existing custom-glob value untouched."""
        from fnd.kinds import ALL_KIND_IDS

        picked = [v for v in values if v in set(ALL_KIND_IDS)]
        self._fields["includes"] = [] if set(picked) >= set(ALL_KIND_IDS) else picked
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
            self.app.push_screen(
                TreePickerScreen(item) if item.groups_provider is not None else PickerScreen(item)
            )
        elif item.kind == KIND_SCALAR:
            field_key = item.id.split(".", 1)[-1]
            current = self._fields.get(field_key, "")
            self.query_one(EditBar).open(item, str(current or ""))
        elif item.kind == KIND_TOGGLE:
            new = not (item.toggle_getter(self.app) if item.toggle_getter else False)  # type: ignore[arg-type]
            if item.toggle_setter is not None:
                item.toggle_setter(self.app, new)  # type: ignore[arg-type]
            self.query_one(SettingsList).refresh_values()

    @on(SettingsList.Highlighted)
    def _on_field_highlighted(self, ev: SettingsList.Highlighted) -> None:
        """Mirror the SettingsScreen pattern: populate the DetailStrip
        with the focused row's description on cursor move."""
        strip = self.query_one(DetailStrip)
        item = ev.item
        if item is None:
            strip.clear()
            return
        meta = item.hint or ""
        strip.set(item.description or "", meta, markup=item.description_markup)

    @on(TextArea.Changed, "#frontmatter_sample")
    def _on_sample_changed(self, _ev: TextArea.Changed) -> None:
        """Live match-status when the user pastes/edits a frontmatter
        sample — mirrors SourceFormScreen so the wizard's tester is
        actually functional, not just visually present."""
        self._refresh_match_status()

    def _refresh_match_status(self) -> None:
        sample = self.query_one("#frontmatter_sample", TextArea).text
        filter_text = str(self._fields.get("filter") or "").strip()
        status = self.query_one("#match_status", Static)
        status.remove_class("-match")
        status.remove_class("-no-match")
        if not sample.strip():
            status.update("(no sample)")
            return
        from fnd.filter_dsl import parse_or_error
        from fnd.frontmatter import FrontmatterParseError, read_frontmatter_from_text

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

    @on(EditBar.EditCommitted)
    def _on_edit_committed(self, ev: EditBar.EditCommitted) -> None:
        field_key = ev.item.id.split(".", 1)[-1]
        if field_key == "filter":
            text = str(ev.value or "").strip()
            if text:
                from fnd.filter_dsl import parse_or_error

                _pred, err = parse_or_error(text)
                if err is not None:
                    self.query_one(EditBar).show_error(f"col {err.column}: {err.message}")
                    return
        self._fields[field_key] = ev.value
        self.query_one(EditBar).close()
        self.query_one(SettingsList).refresh_values()
        # Re-evaluate the sample tester since the filter may have changed.
        self._refresh_match_status()
        self.query_one(SettingsList).focus()

    def action_back(self) -> None:
        self.app.pop_screen()

    def action_save_close(self) -> None:
        from pathlib import Path

        from fnd.config import (
            EXCLUDES_PRESETS,
            CollectionConfig,
            InvalidCollectionNameError,
            SourceConfig,
            default_config_path,
            load,
            validate_collection_name,
            write_collection,
        )

        self._clear_error()

        name = str(self._fields["name"]).strip()
        path = str(self._fields["path"]).strip().strip("'\"")
        if not name:
            self._show_error("Name is required.")
            return
        # Validate up-front so the user sees a focused error instead of a
        # crash from deep inside write_collection if they typed something
        # the persistence layer would reject (path separators, quotes,
        # control chars, …). Spaces ARE allowed — see validate_collection_name.
        try:
            validate_collection_name(name)
        except InvalidCollectionNameError as e:
            self._show_error(str(e))
            return
        if not path:
            self._show_error("Source path is required.")
            return
        p = Path(path).expanduser()
        if not p.exists():
            self._show_error(f"Path does not exist: {p}")
            return

        includes_globs: list[str] = _kinds_to_include_globs(list(self._fields["includes"]))
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

        app: FNDApp = self.app  # type: ignore[assignment]
        cfg = app._config  # type: ignore[attr-defined]
        if cfg is not None and name in cfg.collections:
            self._show_error(f"Collection {name!r} already exists.")
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
        try:
            write_collection(
                config_path=config_path,
                name=name,
                collection=new_collection,
            )
        except InvalidCollectionNameError as e:
            self._show_error(str(e))
            return
        app._config = load()  # type: ignore[attr-defined]
        app._scope.refresh_collections_panel()  # type: ignore[attr-defined]
        # Pop wizard FIRST so the IndexerScreen lands on top of the
        # per-collection menu, not on top of this wizard. Switching the
        # reindex from the headless _reindex_collection_async worker to
        # the unified _reindex_with_warning_if_needed path gives the
        # user the same modal + Cancel + progress they get from
        # 'Update index now'.
        self.app.pop_screen()
        from fnd.tui.menu import _make_open_collection_screen

        _make_open_collection_screen(name)(app)
        app._indexer.reindex_with_warning(name, rebuild=True)  # type: ignore[attr-defined]

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
        app: FNDApp = self.app  # type: ignore[assignment]
        self.query_one("#footer_hints", Static).update(
            _hint_bar(app, (("⏎", "Create"), ("Esc", "Cancel")))
        )

    @on(Input.Submitted, "#new_collection_name")
    def _create(self, ev: Input.Submitted) -> None:
        name = ev.value.strip()
        if not name:
            self.app.pop_screen()
            return
        from fnd.config import CollectionConfig, default_config_path, load, write_collection

        app: FNDApp = self.app  # type: ignore[assignment]
        if app._config and name in app._config.collections:  # type: ignore[attr-defined]
            self.notify(f"Collection {name!r} already exists.", severity="warning")
            return
        write_collection(
            config_path=default_config_path(),
            name=name,
            collection=CollectionConfig(sources=[]),
        )
        app._config = load()  # type: ignore[attr-defined]
        app._scope.refresh_collections_panel()  # type: ignore[attr-defined]
        self.app.pop_screen()

    def action_back(self) -> None:
        self.app.pop_screen()


class RenameCollectionScreen(Screen[None]):
    """Tiny one-Input prompt for renaming a collection.

    Implementation note: there is no atomic "rename collection" in
    `fnd.config`, so this writes the new name (copy of the existing
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
        app: FNDApp = self.app  # type: ignore[assignment]
        self.query_one("#footer_hints", Static).update(
            _hint_bar(app, (("⏎", "Save"), ("Esc", "Cancel")))
        )

    @on(Input.Submitted, "#new_collection_name")
    def _save(self, ev: Input.Submitted) -> None:
        new_name = ev.value.strip()
        if not new_name or new_name == self._old_name:
            self.app.pop_screen()
            return
        from fnd.config import (
            default_config_path,
            delete_collection,
            load,
            write_collection,
        )

        app: FNDApp = self.app  # type: ignore[assignment]
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
        app._scope.refresh_collections_panel()  # type: ignore[attr-defined]
        # Pop twice — past Rename and the now-stale per-collection
        # screen — before pushing the IndexerScreen.
        self.app.pop_screen()
        self.app.pop_screen()
        app._indexer.reindex_with_warning(new_name, rebuild=True)  # type: ignore[attr-defined]

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
    DeleteCollectionScreen { background: $surface; align: center middle; }
    DeleteCollectionScreen > #settings_box {
        width: auto;
        min-width: 60;
        max-width: 100;
        height: auto;
        max-height: 90%;
        border: round $error;
        padding: 0 1;
    }
    DeleteCollectionScreen #confirm_summary { padding: 0 0 1 0; }
    DeleteCollectionScreen #confirm_list { height: auto; }
    DeleteCollectionScreen #deleting_status { padding: 1 0; color: $text-muted; }
    DeleteCollectionScreen #deleting_spinner { height: 1; }
    DeleteCollectionScreen .-hidden { display: none; }
    DeleteCollectionScreen > #footer_hints {
        dock: bottom; height: 1; background: $surface; padding: 0 1; color: $text-muted;
    }
    """

    def __init__(self, *, collection_name: str) -> None:
        super().__init__()
        self._name = collection_name
        # Set once the worker is dispatched: freezes the bindings so the user
        # can't re-fire "Yes" (a second worker) or escape onto the now-stale
        # parent screen mid-delete. Never cleared — the screen is single-use.
        self._deleting = False

    def compose(self) -> ComposeResult:
        with Vertical(id="settings_box") as box:
            box.border_title = f"Collections › {self._name} › Delete"
            yield Static(
                build_confirm_body(
                    outcome=(
                        f"Collection '{self._name}' removed from config; "
                        "its chunks dropped from the search index."
                    ),
                    cost=(
                        "Cannot be reversed. Re-adding the sources and "
                        "running Update index would rebuild."
                    ),
                    safety=(
                        "Source files on disk are untouched. Other collections "
                        "and the PDF Texture Cache are unaffected."
                    ),
                    irreversible=True,
                ),
                id="confirm_summary",
            )
            yield OptionList(
                confirm_yes_option(f"Yes, delete {self._name}", severity="destructive"),
                Option("Cancel", id="no"),
                id="confirm_list",
            )
            # Shown in place of the choices while the index drop runs on a
            # worker; composed up-front and toggled so we never mount/remove
            # mid-run (that races the rendered tree).
            from textual.widgets import LoadingIndicator

            yield Static(
                f"Deleting '{self._name}' from the search index…",
                id="deleting_status",
                classes="-hidden",
            )
            yield LoadingIndicator(id="deleting_spinner", classes="-hidden")
        yield Static("", id="footer_hints")

    def on_mount(self) -> None:
        self.query_one("#confirm_list", OptionList).focus()
        app: FNDApp = self.app  # type: ignore[assignment]
        self.query_one("#footer_hints", Static).update(
            _hint_bar(app, (("⏎", "Confirm"), ("Esc", "Cancel")))
        )

    def action_cursor(self, direction: int) -> None:
        if self._deleting:
            return
        lst = self.query_one("#confirm_list", OptionList)
        if direction > 0:
            lst.action_cursor_down()
        else:
            lst.action_cursor_up()

    def action_activate(self) -> None:
        if self._deleting:
            return
        self.query_one("#confirm_list", OptionList).action_select()

    @on(OptionList.OptionSelected, "#confirm_list")
    def _on_select(self, ev: OptionList.OptionSelected) -> None:
        if ev.option.id == "no":
            self.app.pop_screen()
            return
        # Yes branch. Config writes are quick; the index drop's commit +
        # wait_merging_threads blocks for 95-145ms on a fresh index and
        # seconds on a fragmented one, so it runs on a worker with a spinner.
        # Non-atomic by design: the config is committed here, before the worker
        # drops the chunks. A _drop failure leaves orphan chunks with the
        # collection already gone from config — surfaced to the user, cleared by
        # a later "Rebuild all"; no auto-rollback (matches the prior sync path).
        import contextlib

        from fnd.config import default_config_path, delete_collection, load

        app: FNDApp = self.app  # type: ignore[assignment]
        delete_collection(config_path=default_config_path(), name=self._name)
        app._config = load()  # type: ignore[attr-defined]
        self._show_deleting()
        name = self._name
        index_dir = app._index_dir  # type: ignore[attr-defined]

        def _drop() -> str | None:
            from fnd.index import _ensure_index
            from fnd.schema import F_COLLECTION

            try:
                index = _ensure_index(index_dir)
                writer = index.writer(heap_size=50_000_000)
                writer.delete_documents(F_COLLECTION, name)
                writer.commit()
                writer.wait_merging_threads()
            except Exception as e:
                return str(e)
            return None

        def _work() -> None:
            error = _drop()
            with contextlib.suppress(Exception):
                app.call_from_thread(self._finish_delete, app, error)

        app.run_worker(_work, thread=True, exclusive=True, group=f"delete-{name}")

    def _show_deleting(self) -> None:
        """Swap the confirm choices for the spinner while the worker runs and
        freeze the bindings so the delete can't be re-fired or escaped."""
        import contextlib

        from textual.widgets import LoadingIndicator

        self._deleting = True

        with contextlib.suppress(Exception):
            self.query_one("#confirm_summary", Static).add_class("-hidden")
            self.query_one("#confirm_list", OptionList).add_class("-hidden")
            self.query_one("#deleting_status", Static).remove_class("-hidden")
            self.query_one("#deleting_spinner", LoadingIndicator).remove_class("-hidden")

    def _finish_delete(self, app: FNDApp, error: str | None) -> None:
        """Back on the UI thread: report, refresh, and pop both screens.

        ``app`` is passed in (not read off ``self``) so this survives the
        screen being unmounted mid-delete."""
        import contextlib

        if error:
            app.notify(f"Index drop failed: {error}", severity="error")
        with contextlib.suppress(Exception):
            app._scope.refresh_collections_panel()  # type: ignore[attr-defined]
        # Pop the Delete screen + the now-stale per-collection screen beneath it
        # — but only while the Delete screen is still on top. A manual escape
        # mid-delete means the user already navigated elsewhere, so popping by
        # count would drop unrelated screens. Never pop the root.
        if app.screen is self:
            for _ in range(2):
                if len(app.screen_stack) <= 1:
                    break
                with contextlib.suppress(Exception):
                    app.pop_screen()

    def action_back(self) -> None:
        if self._deleting:
            return
        self.app.pop_screen()


# ── Cache maintenance confirm ───────────────────────────────────────


class CacheMaintenanceConfirm(Screen[None]):
    """Confirm screen for cache prune / clear.

    Mirrors :class:`DeleteCollectionScreen` chrome — same bordered
    settings_box, same OptionList Yes/Cancel pattern, same key
    bindings. Arrows navigate between options; Enter selects.
    Destructive variants use ``$error`` border; reversible variants
    use ``$warning``.
    """

    BINDINGS = [  # noqa: RUF012
        Binding("escape,left", "back", "Cancel", show=False),
        Binding("up,k", "cursor(-1)", show=False),
        Binding("down,j", "cursor(1)", show=False),
        Binding("enter", "activate", show=False),
    ]

    CSS = """
    CacheMaintenanceConfirm { background: $surface; align: center middle; }
    CacheMaintenanceConfirm > #settings_box {
        width: auto;
        min-width: 60;
        max-width: 100;
        height: auto;
        max-height: 90%;
        border: round $warning;
        padding: 0 1;
    }
    CacheMaintenanceConfirm.-destructive > #settings_box { border: round $error; }
    CacheMaintenanceConfirm #confirm_summary { padding: 0 0 1 0; }
    CacheMaintenanceConfirm #confirm_irreversible {
        color: $error; text-style: bold; padding: 0 0 1 0;
    }
    CacheMaintenanceConfirm #confirm_list { height: auto; }
    CacheMaintenanceConfirm > #footer_hints {
        dock: bottom; height: 1; background: $surface; padding: 0 1; color: $text-muted;
    }
    """

    def __init__(
        self,
        *,
        title: str,
        summary: Text,
        run: Callable[[], int],
        confirm_label: str,
        result_label: str,
        irreversible: bool = False,
    ) -> None:
        super().__init__()
        self._title = title
        self._summary = summary
        self._run_callback = run
        self._confirm_label = confirm_label
        self._result_label = result_label
        self._irreversible = irreversible
        if irreversible:
            self.add_class("-destructive")

    def compose(self) -> ComposeResult:
        with Vertical(id="settings_box") as box:
            box.border_title = self._title
            yield Static(self._summary, id="confirm_summary")
            if self._irreversible:
                yield Static("⚠  Cannot be undone.", id="confirm_irreversible")
            yield OptionList(
                Option(Text(self._confirm_label, style="bold"), id="yes"),
                Option("Cancel", id="no"),
                id="confirm_list",
            )
        yield Static("", id="footer_hints")

    def on_mount(self) -> None:
        self.query_one("#confirm_list", OptionList).focus()
        app: FNDApp = self.app  # type: ignore[assignment]
        self.query_one("#footer_hints", Static).update(
            _hint_bar(app, (("↑↓", "Nav"), ("⏎", "Confirm"), ("Esc", "Cancel")))
        )

    def action_cursor(self, direction: int) -> None:
        lst = self.query_one("#confirm_list", OptionList)
        if direction > 0:
            lst.action_cursor_down()
        else:
            lst.action_cursor_up()

    def action_activate(self) -> None:
        self.query_one("#confirm_list", OptionList).action_select()

    def action_back(self) -> None:
        self.app.pop_screen()

    @on(OptionList.OptionSelected, "#confirm_list")
    def _on_select(self, ev: OptionList.OptionSelected) -> None:
        if ev.option.id == "no":
            self.app.pop_screen()
            return
        try:
            n = self._run_callback()
        except Exception as e:
            self.notify(f"Failed: {e}", severity="error")
            self.app.pop_screen()
            return
        self.notify(f"✓ {n} {self._result_label}.", timeout=5)
        self.app.pop_screen()


# ── Update all collections confirm ──────────────────────────────────


class UpdateAllConfirm(Screen[None]):
    """Confirm + chain Update index across every collection.

    Mirrors the CacheMaintenanceConfirm chrome — bordered box,
    OptionList Yes/Cancel, hint bar. On Yes, kicks off the first
    collection's update via the existing per-collection modal path;
    when that completes, advances to the next. Phase F adds a
    proper aggregate progress modal — for now we delegate to
    sequential per-collection runs."""

    BINDINGS = [  # noqa: RUF012
        Binding("escape,left", "back", "Cancel", show=False),
        Binding("up,k", "cursor(-1)", show=False),
        Binding("down,j", "cursor(1)", show=False),
        Binding("enter", "activate", show=False),
    ]

    CSS = """
    UpdateAllConfirm { background: $surface; align: center middle; }
    UpdateAllConfirm > #settings_box {
        width: auto;
        min-width: 60;
        max-width: 100;
        height: auto;
        max-height: 90%;
        border: round $primary 50%;
        padding: 0 1;
    }
    UpdateAllConfirm #confirm_summary { padding: 0 0 1 0; }
    UpdateAllConfirm #confirm_list { height: auto; }
    UpdateAllConfirm > #footer_hints {
        dock: bottom; height: 1; background: $surface; padding: 0 1; color: $text-muted;
    }
    """

    def __init__(
        self,
        *,
        collection_names: list[str],
        texturise_override: bool | None = None,
        skip_unchanged: bool = True,
        force_fresh: bool = False,
        rebuild: bool = False,
    ) -> None:
        super().__init__()
        self._names = list(collection_names)
        # None = follow the toggle (the original action), True = always
        # texturise (the shared "Update everything" action), False =
        # never texturise (the "Process new files index-only" action).
        self._texturise_override = texturise_override
        # rebuild=True + force_fresh=True + skip_unchanged=False is the
        # "Rebuild all collections" action: drop each collection's chunks
        # and re-extract every file fresh. Otherwise indexing is
        # incremental and reuses existing texturising.
        self._skip_unchanged = skip_unchanged
        self._force_fresh = force_fresh
        self._rebuild = rebuild

    def _mode_label(self) -> str:
        if self._rebuild:
            return "Rebuild — drop chunks and re-texturise every PDF from scratch"
        if self._force_fresh:
            return "Re-texturise documents on an older engine version"
        if self._texturise_override is True:
            return "Index + texturise (toggle ignored)"
        if self._texturise_override is False:
            return "Index only - skip texturising (toggle ignored)"
        return "Follow Texturise-while-indexing toggle"

    def compose(self) -> ComposeResult:
        with Vertical(id="settings_box") as box:
            box.border_title = f"Collections › Update all ({len(self._names)})"
            text = Text()
            text.append("Queue     ", style="dim")
            # List the collections so the user can see exactly what
            # will run, not just a count.
            text.append(", ".join(self._names))
            text.append("\n")
            text.append("Mode      ", style="dim")
            text.append(self._mode_label())
            text.append("\n")
            text.append("Per file  ", style="dim")
            if self._rebuild:
                text.append(
                    "Every PDF is re-texturised from scratch (cache bypassed). "
                    "Costly — use to rebuild all previews under the current engine.\n"
                )
            elif self._force_fresh:
                text.append(
                    "Every file is revisited; up-to-date texturising is reused, "
                    "only older-engine versions are re-extracted.\n"
                )
            else:
                text.append(
                    "Unchanged files are skipped. The PDF Texture Cache is consulted, not cleared.\n"
                )
            text.append("Order     ", style="dim")
            text.append("Sequential. Each shows its own progress; queue advances on completion.\n")
            yield Static(text, id="confirm_summary")
            confirm = f"Yes, update all {len(self._names)} collections"
            yield OptionList(
                Option(Text(confirm, style="bold green"), id="yes"),
                Option("Cancel", id="no"),
                id="confirm_list",
            )
        yield Static("", id="footer_hints")

    def on_mount(self) -> None:
        self.query_one("#confirm_list", OptionList).focus()
        app: FNDApp = self.app  # type: ignore[assignment]
        self.query_one("#footer_hints", Static).update(
            _hint_bar(app, (("↑↓", "Nav"), ("⏎", "Confirm"), ("Esc", "Cancel")))
        )

    def action_cursor(self, direction: int) -> None:
        lst = self.query_one("#confirm_list", OptionList)
        if direction > 0:
            lst.action_cursor_down()
        else:
            lst.action_cursor_up()

    def action_activate(self) -> None:
        self.query_one("#confirm_list", OptionList).action_select()

    def action_back(self) -> None:
        self.app.pop_screen()

    @on(OptionList.OptionSelected, "#confirm_list")
    def _on_select(self, ev: OptionList.OptionSelected) -> None:
        if ev.option.id == "no":
            self.app.pop_screen()
            return
        # Pop the confirm, queue every remaining collection on the
        # app, then trigger the first one. drive_indexer in
        # fnd/tui/indexer_modal advances the chain as each completes.
        names = list(self._names)
        self.app.pop_screen()
        if not names:
            return
        app: FNDApp = self.app  # type: ignore[assignment]
        # First in the queue runs now; the rest queue up for chaining.
        # reindex_with_warning seeds the chain queue from these (and
        # chain_total is preserved so the IndexerScreen title can show
        # "papers (1 of 5)" even after rest has been depleted).
        first, rest = names[0], names[1:]
        try:
            app._indexer.reindex_with_warning(  # type: ignore[attr-defined]
                first,
                texturise_override=self._texturise_override,
                skip_unchanged=self._skip_unchanged,
                force_fresh=self._force_fresh,
                rebuild=self._rebuild,
                chain_remaining=rest,
                chain_total=len(names),
            )
        except Exception:
            self.notify(f"Could not start Update index for {first}", severity="error")


# ── Structured PDF install/uninstall confirm ────────────────────────


def _pdf_cache_size_human() -> str:
    """Human-readable on-disk size of the PDF structure cache, or
    "empty" when the directory doesn't exist yet."""
    from fnd.cache import ExtractionCache, default_cache_dir

    root = default_cache_dir()
    if not root.exists():
        return "empty"
    cache = ExtractionCache()
    n = cache.total_size_bytes()
    if n < 1024:
        return f"{n} B"
    kb = n / 1024
    if kb < 1024:
        return f"{kb:.0f} KB"
    mb = kb / 1024
    if mb < 1024:
        return f"{mb:.0f} MB"
    return f"{mb / 1024:.1f} GB"


class StructuredPdfConfirmScreen(Screen[None]):
    """Disclosure + Yes/Cancel for the pdf-structure extra.

    Mirrors :class:`CacheMaintenanceConfirm` chrome — bordered
    settings_box, OptionList Yes/Cancel, hint bar. State at mount
    decides install vs uninstall copy. Confirming pushes the progress
    modal wired in step 6b.
    """

    BINDINGS = [  # noqa: RUF012
        Binding("escape,left", "back", "Cancel", show=False),
        Binding("up,k", "cursor(-1)", show=False),
        Binding("down,j", "cursor(1)", show=False),
        Binding("enter", "activate", show=False),
    ]

    CSS = """
    StructuredPdfConfirmScreen { background: $surface; align: center middle; }
    StructuredPdfConfirmScreen > #settings_box {
        width: auto;
        min-width: 60;
        max-width: 100;
        height: auto;
        max-height: 90%;
        border: round $primary 50%;
        padding: 0 1;
    }
    StructuredPdfConfirmScreen.-recoverable > #settings_box { border: round $warning; }
    StructuredPdfConfirmScreen.-destructive > #settings_box { border: round $error; }
    StructuredPdfConfirmScreen.-safe > #settings_box { border: round $primary 50%; }
    StructuredPdfConfirmScreen #confirm_summary { padding: 0 0 1 0; }
    StructuredPdfConfirmScreen #confirm_list { height: auto; }
    StructuredPdfConfirmScreen > #footer_hints {
        dock: bottom; height: 1; background: $surface; padding: 0 1; color: $text-muted;
    }
    """

    def __init__(self) -> None:
        super().__init__()
        from fnd.extras import EXTRAS

        self._extra = EXTRAS.get("pdf-structure")
        self._installed = self._extra is not None and self._is_installed()
        # Install is "safe" (additive, reversible). Uninstall is
        # "recoverable" — packages go but indexed chunks stay, so the
        # user can recover by reinstalling.
        self._severity = "recoverable" if self._installed else "safe"
        self.add_class(confirm_border_class(self._severity))

    def _is_installed(self) -> bool:
        from fnd.extras import is_extra_installed

        return self._extra is not None and is_extra_installed(self._extra)

    def compose(self) -> ComposeResult:
        title = (
            "Indexing › PDF Texturising › Uninstall engine"
            if self._installed
            else "Indexing › PDF Texturising › Install engine"
        )
        with Vertical(id="settings_box") as box:
            box.border_title = title
            yield Static(self._summary_text(), id="confirm_summary")
            confirm_label = (
                "Yes, uninstall the texturising engine"
                if self._installed
                else "Yes, install the texturising engine"
            )
            yield OptionList(
                confirm_yes_option(confirm_label, severity=self._severity),
                Option("Cancel", id="no"),
                id="confirm_list",
            )
        yield Static("", id="footer_hints")

    def _summary_text(self) -> Text:
        from fnd.extras import actual_disk_mb

        if self._extra is None:
            return Text("Texturising engine is unavailable.", style="bold red")
        if self._installed:
            # Uninstall is a give-back action: framing is "what
            # changes / what you get back / what's preserved." The
            # PDF Texture Cache stays - it's a separate concept - so
            # spell that out so the user isn't surprised by leftover
            # disk usage.
            cache_size = _pdf_cache_size_human()
            cache_line = (
                f"PDF Texture Cache ({cache_size}) stays. "
                "Clear it separately via Settings → Indexing → "
                "Clear texture cache."
            )
            return build_confirm_body(
                outcome_label="What changes",
                outcome="New PDFs render as flat text in the preview pane.",
                cost_label="Disk freed",
                cost=f"~{actual_disk_mb(self._extra)} MB (packages).",
                safety_label="Preserved",
                safety=(
                    "Already-textured PDFs keep rendering with structure "
                    "until the next Update index. " + cache_line
                ),
            )
        from fnd.tui.cost_estimate import estimate_per_pdf_seconds, has_calibration_data

        total_mb = sum(p.disk_mb for p in self._extra.packages)
        secs_per_pdf = estimate_per_pdf_seconds()
        first_run_note = f"First Update index spends about {secs_per_pdf:.1f} s per PDF " + (
            "on your machine." if has_calibration_data() else "(rough estimate)."
        )
        return build_confirm_body(
            outcome=("PDFs gain structured preview rendering (headings, lists, tables)."),
            cost=(f"~{total_mb} MB disk + ML weights on first use. " + first_run_note),
            safety="Auto-resumes if interrupted. Already-indexed PDFs keep working.",
        )

    def on_mount(self) -> None:
        self.query_one("#confirm_list", OptionList).focus()
        app: FNDApp = self.app  # type: ignore[assignment]
        self.query_one("#footer_hints", Static).update(
            _hint_bar(app, (("↑↓", "Nav"), ("⏎", "Confirm"), ("Esc", "Cancel")))
        )

    def action_cursor(self, direction: int) -> None:
        lst = self.query_one("#confirm_list", OptionList)
        if direction > 0:
            lst.action_cursor_down()
        else:
            lst.action_cursor_up()

    def action_activate(self) -> None:
        self.query_one("#confirm_list", OptionList).action_select()

    def action_back(self) -> None:
        self.app.pop_screen()

    @on(OptionList.OptionSelected, "#confirm_list")
    def _on_select(self, ev: OptionList.OptionSelected) -> None:
        if ev.option.id == "no" or self._extra is None:
            self.app.pop_screen()
            return
        import sys

        from fnd.extras import (
            _project_pyproject_for_python,  # type: ignore[attr-defined]
            disable_pdf_structure_default_group,
            enable_pdf_structure_default_group,
            install_commands,
            uninstall_commands,
        )
        from fnd.tui.extras_install_progress import start_extras_install

        # When fnd is running inside a uv-managed project venv, toggle
        # the ``pdf-structure`` group in ``[tool.uv] default-groups``
        # BEFORE running the sync. Otherwise a subsequent ``uv sync``
        # would wipe the install (extras / non-default groups are
        # removed when not flagged active).
        pyproject = _project_pyproject_for_python(sys.executable)

        if self._installed:
            if pyproject is not None and self._extra.name == "pdf-structure":
                disable_pdf_structure_default_group(pyproject)
            cmds = uninstall_commands(self._extra)
            label = "Uninstall"
        else:
            if pyproject is not None and self._extra.name == "pdf-structure":
                enable_pdf_structure_default_group(pyproject)
            cmds = install_commands(self._extra)
            label = "Install"
        app: FNDApp = self.app  # type: ignore[assignment]
        self.app.pop_screen()
        start_extras_install(app, cmds=cmds, action_label=label)


# ── Clone-source flow (Phase 5) ─────────────────────────────────────


class DeleteSourceScreen(Screen[None]):
    """Confirm + remove a single source from a collection.

    Triggered by ``Ctrl+D`` inside :class:`SourceFormScreen` (only when
    editing an existing source). The source's path is dropped from
    ``[collections.<name>.sources]`` via :func:`fnd.config.write_collection`.
    Reindex of the collection follows because the source set changed.
    """

    BINDINGS = [  # noqa: RUF012
        Binding("escape,left", "back", "Cancel", show=False),
        Binding("up,k", "cursor(-1)", show=False),
        Binding("down,j", "cursor(1)", show=False),
        Binding("enter", "activate", show=False),
    ]

    CSS = DeleteCollectionScreen.CSS

    def __init__(self, *, collection_name: str, source_index: int) -> None:
        super().__init__()
        self._collection_name = collection_name
        self._source_index = source_index

    def compose(self) -> ComposeResult:
        app: FNDApp = self.app  # type: ignore[assignment]
        cfg = app._config  # type: ignore[attr-defined]
        path_display = "(unknown)"
        if (
            cfg is not None
            and self._collection_name in cfg.collections
            and 0 <= self._source_index < len(cfg.collections[self._collection_name].sources)
        ):
            src = cfg.collections[self._collection_name].sources[self._source_index]
            path_display = str(src.path) or "(no path)"

        with Vertical(id="settings_box") as box:
            box.border_title = (
                f"Collections › {self._collection_name} › Sources › "
                f"Source {self._source_index + 1} › Delete"
            )
            yield Static(
                f"Remove this source from {self._collection_name!r}?\n"
                f"Path: {path_display}\n\n"
                "Only the config entry is removed. The files on disk are "
                "untouched. Indexed chunks for files only reachable via "
                "this source become orphaned until the next reindex.",
                classes="warning",
            )
            yield OptionList(
                Option(Text("Yes, remove this source", style="bold"), id="yes"),
                Option("Cancel", id="no"),
                id="confirm_list",
            )
        yield Static("", id="footer_hints")

    def on_mount(self) -> None:
        self.query_one("#confirm_list", OptionList).focus()
        app: FNDApp = self.app  # type: ignore[assignment]
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

    def action_back(self) -> None:
        self.app.pop_screen()

    @on(OptionList.OptionSelected, "#confirm_list")
    def _on_select(self, ev: OptionList.OptionSelected) -> None:
        if ev.option.id == "no":
            self.app.pop_screen()
            return
        from fnd.config import default_config_path, load, write_collection

        app: FNDApp = self.app  # type: ignore[assignment]
        cfg = app._config  # type: ignore[attr-defined]
        if cfg is None or self._collection_name not in cfg.collections:
            self.notify("Collection vanished", severity="error")
            self.app.pop_screen()
            return
        col = cfg.collections[self._collection_name]
        if not 0 <= self._source_index < len(col.sources):
            self.notify("Source vanished", severity="error")
            self.app.pop_screen()
            return
        del col.sources[self._source_index]
        try:
            write_collection(
                config_path=default_config_path(),
                name=self._collection_name,
                collection=col,
            )
        except Exception as e:
            self.notify(f"Delete failed: {e}", severity="error")
            return
        app._config = load()  # type: ignore[attr-defined]
        app._scope.refresh_collections_panel()  # type: ignore[attr-defined]
        # Pop DeleteSourceScreen AND the now-stale SourceFormScreen
        # below it — land back on the Sources screen — then trigger
        # the reindex so the IndexerScreen mounts on the right
        # parent.
        self.app.pop_screen()
        self.app.pop_screen()
        import contextlib

        with contextlib.suppress(Exception):
            app._indexer.reindex_with_warning(  # type: ignore[attr-defined]
                self._collection_name, rebuild=True
            )


class CloneSourcePickCollectionScreen(Screen[None]):
    """Step 1 of clone: pick the source collection to copy a source FROM.

    Lists every collection except the target so users can't accidentally
    clone from a collection into itself. Enter pushes
    :class:`CloneSourcePickSourceScreen` for that collection.
    """

    BINDINGS = [  # noqa: RUF012
        Binding("escape,left", "back", "Cancel", show=False),
        Binding("up,k", "cursor(-1)", show=False),
        Binding("down,j", "cursor(1)", show=False),
        Binding("enter", "activate", show=False),
    ]

    CSS = """
    CloneSourcePickCollectionScreen { background: $surface; }
    CloneSourcePickCollectionScreen > #settings_box {
        height: auto;
        border: round $primary 50%;
        padding: 0 1;
        margin: 1 4;
    }
    CloneSourcePickCollectionScreen > #settings_box:focus-within { border: round $accent; }
    CloneSourcePickCollectionScreen #clone_list { height: auto; }
    CloneSourcePickCollectionScreen .info { color: $text-muted; padding: 0 0 1 0; }
    CloneSourcePickCollectionScreen > #footer_hints {
        dock: bottom; height: 1; background: $surface; padding: 0 1; color: $text-muted;
    }
    """

    def __init__(self, *, target_collection: str) -> None:
        super().__init__()
        self._target = target_collection

    def compose(self) -> ComposeResult:
        app: FNDApp = self.app  # type: ignore[assignment]
        cfg = app._config  # type: ignore[attr-defined]
        with Vertical(id="settings_box") as box:
            box.border_title = f"Collections › {self._target} › Sources › Clone from…"
            yield Static(
                "Pick a collection to clone a source from. The source is "
                f"deep-copied into {self._target!r} (edits won't propagate).",
                classes="info",
            )
            options: list[Option] = []
            if cfg is not None:
                for name in sorted(cfg.collections):
                    if name == self._target:
                        continue
                    n = len(cfg.collections[name].sources)
                    label = f"{name}  ({n} source{'s' if n != 1 else ''})"
                    options.append(Option(label, id=name))
            if not options:
                options.append(Option("(no other collections)", id="__empty__"))
            yield OptionList(*options, id="clone_list")
        yield Static("", id="footer_hints")

    def on_mount(self) -> None:
        self.query_one("#clone_list", OptionList).focus()
        app: FNDApp = self.app  # type: ignore[assignment]
        self.query_one("#footer_hints", Static).update(
            _hint_bar(app, (("⏎", "Pick"), ("Esc", "Cancel")))
        )

    def action_back(self) -> None:
        self.app.pop_screen()

    def action_cursor(self, direction: int) -> None:
        lst = self.query_one("#clone_list", OptionList)
        if direction > 0:
            lst.action_cursor_down()
        else:
            lst.action_cursor_up()

    def action_activate(self) -> None:
        self.query_one("#clone_list", OptionList).action_select()

    @on(OptionList.OptionSelected, "#clone_list")
    def _on_select(self, ev: OptionList.OptionSelected) -> None:
        if ev.option.id == "__empty__":
            return
        # Push the step-2 picker for the chosen source collection.
        chosen = ev.option.id
        if chosen is None:
            return
        self.app.push_screen(
            CloneSourcePickSourceScreen(
                source_collection=chosen,
                target_collection=self._target,
            )
        )


class CloneSourcePickSourceScreen(Screen[None]):
    """Step 2 of clone: pick the individual source to copy.

    Lists every source in the chosen source collection with a brief
    summary (file types + path). Enter dispatches
    :func:`fnd.config.clone_source` and pops back to the Sources screen
    of the target collection. Triggers a reindex of the target.
    """

    BINDINGS = [  # noqa: RUF012
        Binding("escape,left", "back", "Cancel", show=False),
        Binding("up,k", "cursor(-1)", show=False),
        Binding("down,j", "cursor(1)", show=False),
        Binding("enter", "activate", show=False),
    ]

    CSS = CloneSourcePickCollectionScreen.CSS

    def __init__(self, *, source_collection: str, target_collection: str) -> None:
        super().__init__()
        self._source_coll = source_collection
        self._target = target_collection

    def compose(self) -> ComposeResult:
        app: FNDApp = self.app  # type: ignore[assignment]
        cfg = app._config  # type: ignore[attr-defined]
        with Vertical(id="settings_box") as box:
            box.border_title = (
                f"Collections › {self._target} › Sources › Clone from {self._source_coll}"
            )
            yield Static(
                f"Pick a source from {self._source_coll!r} to deep-copy into {self._target!r}.",
                classes="info",
            )
            options: list[Option] = []
            if cfg is not None and self._source_coll in cfg.collections:
                sources = cfg.collections[self._source_coll].sources
                for i, src in enumerate(sources):
                    base = Path(str(src.path)).name or str(src.path)
                    types = (
                        ", ".join(
                            ext
                            for ext in ("md", "pdf", "docx", "pptx", "txt")
                            if any(g.endswith(f".{ext}") for g in src.includes)
                        )
                        or "all"
                    )
                    label = f"{i + 1}. {base}  ·  {types}  ·  {src.path}"
                    options.append(Option(label, id=str(i)))
            if not options:
                options.append(Option("(collection has no sources)", id="__empty__"))
            yield OptionList(*options, id="clone_list")
        yield Static("", id="footer_hints")

    def on_mount(self) -> None:
        self.query_one("#clone_list", OptionList).focus()
        app: FNDApp = self.app  # type: ignore[assignment]
        self.query_one("#footer_hints", Static).update(
            _hint_bar(app, (("⏎", "Clone"), ("Esc", "Cancel")))
        )

    def action_back(self) -> None:
        self.app.pop_screen()

    def action_cursor(self, direction: int) -> None:
        lst = self.query_one("#clone_list", OptionList)
        if direction > 0:
            lst.action_cursor_down()
        else:
            lst.action_cursor_up()

    def action_activate(self) -> None:
        self.query_one("#clone_list", OptionList).action_select()

    @on(OptionList.OptionSelected, "#clone_list")
    def _on_select(self, ev: OptionList.OptionSelected) -> None:
        if ev.option.id == "__empty__" or ev.option.id is None:
            return
        try:
            idx = int(ev.option.id)
        except ValueError:
            return
        from fnd.config import clone_source, default_config_path, load

        try:
            clone_source(
                config_path=default_config_path(),
                source_collection=self._source_coll,
                source_index=idx,
                target_collection=self._target,
            )
        except (KeyError, IndexError, ValueError) as e:
            self.notify(f"Clone failed: {e}", severity="error")
            return

        app: FNDApp = self.app  # type: ignore[assignment]
        app._config = load()  # type: ignore[attr-defined]
        app._scope.refresh_collections_panel()  # type: ignore[attr-defined]
        self.notify(
            f"Cloned source from {self._source_coll!r} into {self._target!r}. "
            f"Reindexing {self._target}…",
            title="Clone source",
            timeout=4,
        )
        # Pop both: step-2 picker, then step-1 picker — then trigger
        # the reindex so the IndexerScreen mounts on the right parent.
        self.app.pop_screen()
        self.app.pop_screen()
        import contextlib

        with contextlib.suppress(Exception):
            app._indexer.reindex_with_warning(  # type: ignore[attr-defined]
                self._target, rebuild=True
            )


# ── Public entry points used by the main app ────────────────────────


def open_settings(app: FNDApp) -> None:
    """Open the Settings root menu — a short list of categories the
    user can drill into. No content stacked on a single screen."""
    items = build_root_items(app)
    app.push_screen(
        SettingsScreen(
            breadcrumb=(),
            items=items,
            provider=lambda a: tuple(build_root_items(a)),
        )
    )


def open_settings_section(
    app: FNDApp,
    section_id: str,
    *,
    context_hint: str | None = None,
) -> None:
    """Push a Settings sub-screen directly (no intermediate root push).

    Used by drill-in rows on the root menu AND by the global shortcuts
    (`?` → Keybindings, F3 → Collections). When called from `?`, an
    optional ``context_hint`` is threaded into the section's provider so
    the Keybindings list reorders its sections — most-relevant first
    after Global. Hint is ignored by sections that don't consume it.
    """
    label = section_label(section_id)
    items = section_items(app, section_id, context_hint=context_hint)
    app.push_screen(
        SettingsScreen(
            breadcrumb=(label,),
            items=items,
            provider=lambda a, _s=section_id, _h=context_hint: tuple(
                section_items(a, _s, context_hint=_h)
            ),
        )
    )


# ── Still-flat drill-in ─────────────────────────────────────────────


def _format_recorded_at(iso: str) -> str:
    """Compact local-time label for an ISO-8601 UTC timestamp.

    today HH:MM        — same calendar day
    yesterday HH:MM    — previous calendar day
    Mon HH:MM          — within the last 7 days
    MMM DD HH:MM       — older than a week"""
    import datetime as _dt

    try:
        ts = _dt.datetime.fromisoformat(iso).astimezone()
    except ValueError:
        return iso
    now = _dt.datetime.now().astimezone()
    days = (now.date() - ts.date()).days
    hm = ts.strftime("%H:%M")
    if days == 0:
        return f"today {hm}"
    if days == 1:
        return f"yesterday {hm}"
    if days < 7:
        return f"{ts.strftime('%a')} {hm}"
    return ts.strftime("%b %d %H:%M")


def _flat_pdfs_with_reasons(  # pyright: ignore[reportUnusedFunction]  # consumed cross-module by fnd.tui.flat_pdf_scan + menu, never inline (the scan is too slow for the event loop)
    *, collection: str | None = None
) -> list[tuple[str, str, str, str | None]]:
    """Return a list of ``(collection, path, reason, recorded_at)`` for
    every PDF that is on disk but has no body_md-bearing chunk in the
    tantivy index (i.e. not texturised — body_struct is present on every
    indexed PDF and can't distinguish flat from textured). ``recorded_at``
    is the failure-log timestamp, or None when inferred. Reasons are
    sourced from the failure log when present; otherwise inferred from
    current state (engine off / battery-saver toggle / unknown)."""
    import contextlib
    from pathlib import Path

    import tantivy

    from fnd.config import default_index_dir, load
    from fnd.schema import F_BODY_MD, F_COLLECTION, F_KIND, F_PATH
    from fnd.tui.failure_log import list_failures

    cfg = load()
    target_cols = [collection] if collection is not None else list(cfg.collections)
    # Build per-collection on-disk PDF inventories using the SAME
    # filter chain the indexer uses (includes/excludes + frontmatter).
    # Earlier the function did a naive ``root.rglob('*.pdf')`` which
    # included every PDF physically under the source root regardless of
    # the source's ``includes: ['**/*.md']`` restriction or its
    # ``frontmatter_filter``. A PDF the user explicitly scoped OUT of
    # a collection would then show up forever in that collection's
    # Flat PDFs list as "still flat" - the indexer can't index
    # what isn't in its walk, so the file would never be cleared from
    # the log no matter how many Updates the user ran.
    from fnd.walk import walk_sources

    on_disk: dict[str, set[str]] = {}
    for name in target_cols:
        col = cfg.collections.get(name)
        if col is None:
            continue
        paths: set[str] = set()
        for path in walk_sources(sources=list(col.sources)):
            if path.suffix.lower() != ".pdf":
                continue
            with contextlib.suppress(OSError):
                paths.add(str(path.resolve()))
        on_disk[name] = paths

    # Per-collection textured-path sets via tantivy.
    textured: dict[str, set[str]] = {name: set() for name in on_disk}
    index_dir = default_index_dir()
    if index_dir.exists():
        try:
            index = tantivy.Index.open(str(index_dir))
            index.reload()
            searcher = index.searcher()
            for name in on_disk:
                col_q = tantivy.Query.term_query(index.schema, F_COLLECTION, name)
                pdf_q = tantivy.Query.boolean_query(
                    [
                        (tantivy.Occur.Must, col_q),
                        (tantivy.Occur.Must, tantivy.Query.term_query(index.schema, F_KIND, "pdf")),
                    ]
                )
                for _score, addr in searcher.search(pdf_q, limit=200000).hits:
                    doc = searcher.doc(addr)
                    # body_md is the texturing payload; body_struct (flat
                    # Blocks) is on every indexed PDF and can't tell flat
                    # from textured.
                    if not doc.get_first(F_BODY_MD):  # type: ignore[attr-defined]
                        continue
                    p = doc.get_first(F_PATH)  # type: ignore[attr-defined]
                    if p:
                        with contextlib.suppress(OSError):
                            textured[name].add(str(Path(str(p)).resolve()))
        except Exception:
            pass

    # Failure-log records keyed by (collection, path).
    failure_by_key: dict[tuple[str, str], tuple[str, str]] = {}
    for r in list_failures():
        with contextlib.suppress(OSError):
            failure_by_key[(r.collection, str(Path(r.path).resolve()))] = (
                r.reason,
                r.recorded_at,
            )

    # Reason fallback when no failure record exists.
    from fnd.tui.menu import _is_pdf_structure_installed

    engine_on = _is_pdf_structure_installed()
    try:
        full_cfg = load()
        battery_saver = not bool(full_cfg.defaults.cache_at_index_time)
    except Exception:
        battery_saver = False

    # Drop user-dismissed PDFs - those are files the user has
    # explicitly accepted as "fine flat" and don't want pestered
    # about every time the log opens.
    from fnd.cache import sha256_file as _sha256_file
    from fnd.dismissed_pdfs import is_dismissed as _is_dismissed

    out: list[tuple[str, str, str, str | None]] = []
    for name, paths in on_disk.items():
        flat = paths - textured.get(name, set())
        for p in sorted(flat):
            with contextlib.suppress(OSError):
                if _is_dismissed(_sha256_file(Path(p))):
                    continue
            record = failure_by_key.get((name, p))
            if record is None:
                if not engine_on:
                    reason = "Texturising engine is not installed"
                elif battery_saver:
                    reason = "Texturise-while-indexing toggle is OFF"
                else:
                    reason = (
                        "Extraction yielded no structured content. "
                        "Likely a scanned PDF or one with no extractable text."
                    )
                out.append((name, p, reason, None))
            else:
                out.append((name, p, record[0], record[1]))
    return out


class StillFlatDrillIn(Screen[None]):
    """List of every PDF whose preview is flat, grouped one row per file
    with its reason and a Retry action.

    Retry re-runs Update for the file's collection with texturising
    forced on; the cache short-circuits already-textured PDFs so the
    cost is roughly one texturising pass per still-flat PDF."""

    BINDINGS = [  # noqa: RUF012
        Binding("escape,left", "back", "Back", show=False),
        Binding("up,k", "move(-1)", show=False),
        Binding("down,j", "move(1)", show=False),
        Binding("enter,r", "retry", "Retry", show=True),
        Binding("shift+enter", "reveal", "Reveal", show=True),
        Binding("d", "dismiss_pdf", "Dismiss", show=True),
        Binding("c", "copy_path", "Copy path", show=True),
    ]

    CSS = """
    StillFlatDrillIn { background: $surface; }
    StillFlatDrillIn > #settings_box {
        height: 1fr;
        border: round $primary 50%;
        padding: 0 1;
    }
    StillFlatDrillIn > #settings_box:focus-within { border: round $accent; }
    StillFlatDrillIn #empty_state { padding: 1 1; color: $text-muted; }
    StillFlatDrillIn .row { height: auto; padding: 1 1 0 1; }
    StillFlatDrillIn .row.-cursor { background: $accent 20%; }
    StillFlatDrillIn > #footer_hints {
        dock: bottom; height: 1; background: $surface; padding: 0 1; color: $text-muted;
    }
    """

    def __init__(self, *, collection: str | None = None) -> None:
        super().__init__()
        self._collection_filter = collection
        self._rows: list[tuple[str, str, str, str | None]] = []
        self._cursor = 0

    def compose(self) -> ComposeResult:
        title = "Flat PDFs — review & retry"
        if self._collection_filter:
            title += f" - {self._collection_filter}"
        with Vertical(id="settings_box") as box:
            box.border_title = title
            # ``can_focus=False`` keeps the scroll container out of the
            # focus chain so the screen-level Up/Down bindings fire
            # for row navigation - the default focusable VerticalScroll
            # eats arrows for its own scroll handling and the bindings
            # never get a chance to run.
            scroll = VerticalScroll(id="still_flat_body")
            scroll.can_focus = False
            yield scroll
        yield Static("", id="footer_hints")

    def on_mount(self) -> None:
        import contextlib as _ctx

        self._refresh()
        app: FNDApp = self.app  # type: ignore[assignment]
        with _ctx.suppress(Exception):
            self.query_one("#footer_hints", Static).update(
                _hint_bar(
                    app,
                    (
                        ("↑↓", "Nav"),
                        ("⏎ / r", "Retry"),
                        ("⇧⏎", "Reveal"),
                        ("d", "Dismiss"),
                        ("c", "Copy path"),
                        ("Esc", "Back"),
                    ),
                )
            )

    def _refresh(self) -> None:
        """Paint the last scan instantly (or a 'Scanning…' placeholder on
        a cold cache) and recompute off the event loop. The flat-PDF scan
        walks every source on disk and diffs the index — seconds on a real
        corpus — so it must never run on the UI thread or the screen
        freezes on open. ``_on_rows_ready`` repaints when the worker lands."""
        from fnd.tui import flat_pdf_scan

        cached = flat_pdf_scan.cached_rows(self._collection_filter)
        if cached is not None:
            self._render_rows(cached)
            # Only rescan when stale — a fresh cache would otherwise notify
            # _on_rows_ready inline and rebuild the whole row list a second
            # time (flicker + wasted work).
            if not flat_pdf_scan.is_fresh(self._collection_filter):
                self._schedule_rescan()
        else:
            self._render_placeholder("Scanning for flat PDFs…")
            self._schedule_rescan()

    def _schedule_rescan(self) -> None:
        from fnd.tui import flat_pdf_scan

        flat_pdf_scan.schedule_refresh(
            self.app, self._collection_filter, on_ready=self._on_rows_ready
        )

    def _on_rows_ready(self, rows: list[tuple[str, str, str, str | None]]) -> None:
        """Background scan finished (marshalled onto the UI thread)."""
        self._render_rows(rows)

    def _render_placeholder(self, text: str) -> None:
        import contextlib as _ctx

        with _ctx.suppress(Exception):
            body = self.query_one("#still_flat_body", VerticalScroll)
            for child in list(body.children):
                child.remove()
            body.mount(Static(text, id="empty_state"))

    def _render_rows(self, rows: list[tuple[str, str, str, str | None]]) -> None:
        import contextlib as _ctx

        self._rows = rows
        # Clamp cursor after a row is removed by Retry/Dismiss so the
        # cursor doesn't index past the end.
        if self._cursor >= len(self._rows):
            self._cursor = max(0, len(self._rows) - 1)
        with _ctx.suppress(Exception):
            body = self.query_one("#still_flat_body", VerticalScroll)
            for child in list(body.children):
                child.remove()
            if not self._rows:
                body.mount(Static("Nothing to fix - every PDF is textured.", id="empty_state"))
                return
            for i, (col, path, reason, recorded_at) in enumerate(self._rows):
                cls = "row -cursor" if i == self._cursor else "row"
                body.mount(
                    Static(
                        self._format_row(i, col, path, reason, recorded_at),
                        classes=cls,
                    )
                )

    def _format_row(self, i: int, col: str, path: str, reason: str, recorded_at: str | None) -> str:
        """Multi-line row: filename, then status chip + collection +
        date + page-if-known on a second line, then the wrapped reason
        in dim text. Page info is parsed out of the failure log's
        '[last page beat: N/M]' marker so the user knows where the
        worker wedged."""
        import re

        cursor = "▸" if i == self._cursor else " "
        name = Path(path).name

        # Status chip - "failed" when a failure record exists,
        # "still flat" otherwise. Failed gets a red ✗; still-flat
        # gets a yellow ⚠.
        chip = "[red]✗ failed[/]" if recorded_at is not None else "[yellow]⚠ still flat[/]"

        # Pull "[last page beat: N/M]" out of the reason so we can
        # render the page hint separately and clean the reason text.
        page_part = ""
        clean_reason = reason
        page_match = re.search(r"\[last page beat:\s*(\d+)/(\d+)\]", reason)
        if page_match:
            page_part = f"  ·  page {page_match.group(1)}/{page_match.group(2)}"
            clean_reason = re.sub(r"\s*\[last page beat:[^\]]+\]\s*", " ", reason).strip()

        # Meta line: status, collection, date (only for actual
        # failure records; cache-flat files have no recorded run).
        # Bare ``col`` would be eaten by Rich markup as ``[col]`` so
        # we render the collection name as a plain dim chip.
        meta_bits = [chip, f"[dim]{col}[/]"]
        if recorded_at:
            meta_bits.append(f"[dim]{_format_recorded_at(recorded_at)}[/]")
        meta_str = "  ·  ".join(meta_bits) + page_part

        header = f"{cursor} [bold]{name}[/]"
        meta = f"     {meta_str}"
        body = f"     [dim]{clean_reason}[/]"
        return f"{header}\n{meta}\n{body}"

    def action_move(self, delta: int) -> None:
        if not self._rows:
            return
        self._cursor = max(0, min(len(self._rows) - 1, self._cursor + delta))
        self._refresh_cursor()

    def _refresh_cursor(self) -> None:
        body = self.query_one("#still_flat_body", VerticalScroll)
        rows = list(body.query(Static))
        for i, row in enumerate(rows):
            if i >= len(self._rows):
                continue
            col, path, reason, recorded_at = self._rows[i]
            row.update(self._format_row(i, col, path, reason, recorded_at))
            # Move the -cursor class too so the background tint
            # follows the active row, not just the ▸ character.
            if i == self._cursor:
                row.add_class("-cursor")
            else:
                row.remove_class("-cursor")
        # Scroll the active row into view so a long log doesn't
        # leave the cursor off-screen.
        if 0 <= self._cursor < len(rows):
            import contextlib as _ctx

            with _ctx.suppress(Exception):
                body.scroll_to_widget(rows[self._cursor])

    def action_retry(self) -> None:
        if not self._rows:
            return
        col, path, _reason, _recorded_at = self._rows[self._cursor]
        app: FNDApp = self.app  # type: ignore[assignment]
        # Forget THIS file's cache entry before re-running the update,
        # otherwise the next Update cache-hits the previous flat
        # extraction and the file stays flat forever. Per-file
        # precision (instead of forget+run-whole-collection) would need
        # a single-file extract path; routing through the existing
        # per-collection Update is fine because other already-textured
        # PDFs in the collection still short-circuit via cache.
        import contextlib as _ctx

        with _ctx.suppress(Exception):
            from fnd.cache import ExtractionCache, sha256_file
            from fnd.extract.pdf import texture_signature

            cache = ExtractionCache()
            sha = sha256_file(Path(path))
            key = cache.build_key(content_sha256=sha, extractor_signature=texture_signature())
            entry = cache.entry_path(key)
            if entry.exists():
                with _ctx.suppress(OSError):
                    entry.unlink()
        try:
            app._indexer.reindex_with_warning(  # type: ignore[attr-defined]
                col, texturise_override=True
            )
        except Exception as e:
            self.notify(f"Could not start retry for {col}: {e}", severity="error")

    def action_reveal(self) -> None:
        """Reveal the row's PDF in the platform file manager (selected where
        supported), via the OS launcher seam."""
        if not self._rows:
            return
        _col, path, _reason, _recorded_at = self._rows[self._cursor]
        from fnd import launcher

        try:
            launcher.reveal(Path(path))
        except OSError as e:
            self.notify(f"Could not reveal: {e}", severity="error")

    def action_dismiss_pdf(self) -> None:
        """Mark the current row's PDF as 'fine flat - stop showing'.

        Stored content-addressed in fnd.dismissed_pdfs so renaming /
        moving the PDF preserves the dismissal.

        Named ``action_dismiss_pdf`` rather than ``action_dismiss``
        because Textual's ``Screen.action_dismiss`` is the modal-pop
        helper and shadowing it tripped pyright's signature check."""
        if not self._rows:
            return
        col, path, _reason, _recorded_at = self._rows[self._cursor]
        import contextlib as _ctx

        with _ctx.suppress(Exception):
            from fnd.cache import sha256_file
            from fnd.dismissed_pdfs import mark_dismissed
            from fnd.tui.failure_log import clear_failure

            sha = sha256_file(Path(path))
            mark_dismissed(sha)
            with _ctx.suppress(Exception):
                clear_failure(collection=col, path=path)
        # Drop the row optimistically so it vanishes now, then invalidate
        # the cached scan and recompute off-loop to reconcile (a fresh
        # scan within the TTL would otherwise re-render the stale row).
        from fnd.tui import flat_pdf_scan

        self._render_rows([r for r in self._rows if r[1] != path])
        flat_pdf_scan.invalidate(self._collection_filter)
        self._schedule_rescan()
        self.notify(f"Dismissed: {Path(path).name}", severity="information")

    def action_copy_path(self) -> None:
        """Copy the current row's absolute path to the OS clipboard."""
        if not self._rows:
            return
        _col, path, _reason, _recorded_at = self._rows[self._cursor]
        from fnd.tui.clipboard import copy_text

        try:
            copy_text(path)
            self.notify(f"Copied: {path}", severity="information")
        except OSError as e:
            self.notify(f"Could not copy: {e}", severity="error")

    def action_back(self) -> None:
        self.app.pop_screen()
