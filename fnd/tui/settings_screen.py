"""Settings & Commands menu: rendering and dispatch.

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

import contextlib
import copy
import textwrap
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Literal, cast

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
from textual.widgets.option_list import Option, OptionDoesNotExist

from fnd.display_text import sanitise_display_text
from fnd.fsmeta import path_is_absent
from fnd.tui.actions import load_keymap
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
    drill_summary,
    header,
    section_items,
    section_label,
    walk_all_sections,
)
from fnd.tui.widgets import COMMIT_KEY, DetailStrip
from fnd.tui.widgets.clear_bar import RETURN_TO_DEFAULTS, ClearFiltersBar
from fnd.tui.widgets.toggle_tree import ToggleGroup, ToggleItem, ToggleTree

if TYPE_CHECKING:
    from fnd.tui.app import FNDApp


# Width budget for key column in Keys & Actions rows. Anything wider gets
# truncated rather than pushing the description.
_KEY_COL = 12


def _wizard_hints(screen: Any, app: Any) -> Any:
    """The wizard's footer, without the anchors while a box has focus."""
    hints = (
        ("⏎", "Edit"),
        *((("Tab", "Test a sample"),) if len(_focus_targets(screen)) > 1 else ()),
        (COMMIT_KEY, "Save & Index"),
        ("Esc", "Cancel"),
    )
    return _editor_hint_bar(hints) if _typing_in(screen) else _hint_bar(app, hints)


def _focus_targets(screen: Any) -> list[Any]:
    """Panes Tab can reach. The sample tester is hidden without a rule to
    test, and focusing a hidden pane put the cursor somewhere invisible."""
    targets: list[Any] = [screen.query_one(SettingsList)]
    sample = screen.query_one("#frontmatter_sample", TextArea)
    if sample.display:
        targets.append(sample)
    return targets


def _commit_then(screen: Any, resume: Callable[[], None]) -> bool:
    """Land an open edit before saving, and say whether the caller should wait.

    The commit travels as a message, so the value is not in `_fields` until
    the next refresh. If it is rejected the bar stays open showing why, and
    the save does not happen.
    """
    import contextlib

    with contextlib.suppress(Exception):
        bar = screen.query_one(EditBar)
        if bar.is_open:
            bar.commit_pending()

            def _resume() -> None:
                if not screen.query_one(EditBar).is_open:
                    resume()

            screen.call_after_refresh(_resume)
            return True
    return False


def _typing_in(screen: Any) -> bool:
    """Whether a text box on ``screen`` has focus, so the anchors are inert.

    `/`, `:`, `?` and `q` reach a focused box instead of acting, so a footer
    naming them there names keys that do not work.
    """
    import contextlib

    from textual.widgets import Input, TextArea

    with contextlib.suppress(Exception):
        if "-hidden" not in screen.query_one(EditBar).classes:
            return True
    for widget in screen.query(Input):
        if widget.has_focus:
            return True
    return any(widget.has_focus for widget in screen.query(TextArea))


def _editor_hint_bar(contextual: tuple[tuple[str, str], ...]) -> Any:
    """A footer for a screen whose focus is a text box.

    The app's anchors are inert there (`/`, `:`, `?` and `q` type into the
    box), so advertising them names four keys that do not work.
    """
    from fnd.tui.app import render_hint_bar

    return render_hint_bar((), contextual)


def _hint_bar(app: FNDApp, contextual: tuple[tuple[str, str], ...], *, screen: Any = None) -> Any:
    """Build the shared hint-bar Text for a Settings screen. Anchors
    come from the main app (single source of truth), minus ``/``.

    The anchor means "focus the app's query bar", and nowhere in Settings does
    ``/`` do that: on a screen with a row filter it focuses THAT, and on one
    without it does nothing at all. Dropping it only where the filter is
    missing would show ``/ Search`` and ``/ Filter`` in the same footer, one
    key with two labels. The screens that own the key name it themselves, in
    their contextual cluster.

    ``screen`` is accepted for callers that pass it and is not read.
    """
    from fnd.tui.app import render_hint_bar

    anchors: tuple[tuple[str, str], ...] = app._FOOTER_ANCHORS  # type: ignore[attr-defined]
    return render_hint_bar(tuple(a for a in anchors if a[0] != "/"), contextual)


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


class ConfirmList(OptionList):
    """A confirm dialog's Yes/Cancel list, which does not wrap.

    The safe row is the default AND the last one, and a wrapping two-item list
    puts the irreversible row one `Down` away, the reflex that reads a list.
    Both rows stay reachable; only the wrap-around goes.
    """

    def _step(self, direction: Literal[-1, 1]) -> None:
        from textual import _widget_navigation

        landing = _widget_navigation.find_next_enabled_no_wrap(
            self.options, anchor=self.highlighted, direction=direction
        )
        if landing is not None:
            self.highlighted = landing

    def action_cursor_up(self) -> None:
        self._step(-1)

    def action_cursor_down(self) -> None:
        self._step(1)


def open_confirm_list(screen: Screen[Any], *, land_on: str = "") -> tuple[str, str]:
    """Focus a screen's ``#confirm_list``, and say what Enter does from there.

    Irreversible dialogs start on the way out: Enter is one keypress from a
    delete otherwise, and Enter is how every one of these screens is reached.

    The hint is `Select` from every row, because it is computed once at mount
    and nothing recomputes it on a move: a `Confirm` hint on a screen that
    LANDS on the affirmative would stay on screen after one `Down`. A hint
    that follows the highlight would say more, and would need a handler on
    each of the seven screens; this one is true from all of them.
    """
    options = screen.query_one("#confirm_list", OptionList)
    if land_on:
        with contextlib.suppress(OptionDoesNotExist):
            options.highlighted = options.get_option_index(land_on)
    options.focus()
    return ("⏎", "Select")


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


def _display_path(raw: str) -> str:
    """A source path as the user wrote it: `~` rather than a home prefix."""
    from fnd.config_render import under_home

    return under_home(Path(raw).expanduser())


def _discard_custom_globs(screen: Any, field_key: str) -> None:
    """Clear a custom-glob field, keeping the text on offer for the visit.

    The tick is derived from the text, so the value cannot simply stay; but
    dropping it outright would lose typed globs to one keypress, with no undo.
    """
    text = str(screen._fields.get(field_key) or "").strip()
    if not text:
        return
    screen._discarded_globs[field_key] = text
    screen._fields[field_key] = ""
    screen.app.notify(f"Custom globs cleared. Tick again to restore: {text}")


def _custom_seed(screen: Any, field_key: str) -> str:
    """What the glob prompt opens with: the current value, else the one the
    last untick cleared."""
    current = str(screen._fields.get(field_key) or "")
    return current or screen._discarded_globs.get(field_key, "")


# Textual selectors are type selectors and a widget's own CSS is scoped to it,
# so neither `CSS = OtherScreen.CSS` nor a shared class selector matches the
# borrowing screen, which then renders with no background, border or docked footer.
# One definition, stamped with each screen's own name.
_PROMPT_CSS = """
{cls} {{ background: $surface; }}
{cls} > #settings_box {{
    height: auto; border: round $primary 50%; padding: 0 1; margin: 1 4;
}}
{cls} > #settings_box:focus-within {{ border: round $accent; }}
{cls} #clone_list {{ height: auto; }}
{cls} .info {{ color: $text-muted; padding: 0 0 1 0; }}
{cls} > #footer_hints {{
    dock: bottom; height: 1; background: $surface; padding: 0 1; color: $text-muted;
}}
"""

_CONFIRM_CSS = """
{cls} {{ background: $surface; align: center middle; }}
{cls} > #settings_box {{
    width: auto; min-width: 60; max-width: 100; height: auto; max-height: 90%;
    border: round $error; padding: 0 1;
}}
{cls} #confirm_list {{ height: auto; }}
{cls} .warning {{ color: $text-muted; padding: 0 0 1 0; }}
{cls} > #footer_hints {{
    dock: bottom; height: 1; background: $surface; padding: 0 1; color: $text-muted;
}}
"""


def chrome_css(cls: str, *, confirm: bool = False) -> str:
    """The shared Settings chrome, stamped with one screen's type name."""
    return (_CONFIRM_CSS if confirm else _PROMPT_CSS).format(cls=cls)


#: Rows the browser's summary box shows before it scrolls; matches its CSS.
_SUMMARY_ROWS = 5


class _FilterSummary:
    """The browser's summary, capped to the rows its box actually has.

    Fits at paint time rather than on a stored width: the screen's size is not
    settled when the summary is first built, and past five rows the terminal
    cut the expression mid-token with nothing to say it had. Only reachable
    below ~60 columns, which is why a pass at the default width never saw it.
    """

    def __init__(self, head: str, prefix: str, body: str) -> None:
        self._head = head
        self._prefix = prefix
        self._body = body or "no filters"

    def fitted(self, width: int) -> Text:
        # Wrapped, not estimated: a row-count from character arithmetic assumes
        # perfect packing and overflowed the box by a row.
        usable = max(10, width)
        rows_left = max(1, _SUMMARY_ROWS - len(textwrap.wrap(self._head, usable) or [""]))
        rows = textwrap.wrap(self._prefix + self._body, usable) or [self._prefix]
        if len(rows) <= rows_left:
            # Fits: hand back the natural line and let the widget wrap it, so
            # the common case is untouched.
            return Text(str(self))
        rows = rows[:rows_left]
        rows[-1] = rows[-1][: usable - 1].rstrip()[:-1] + "…"
        return Text(self._head + "\n" + "\n".join(rows))

    def __str__(self) -> str:
        return f"{self._head}\n{self._prefix}{self._body}"

    def __rich_console__(self, console: Any, options: Any) -> Any:
        yield self.fitted(options.max_width)


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
    # Sanitised here rather than at the inputs: a path, a filter expression or
    # a tag can also arrive from a hand-edited config, and a tab measures zero
    # cells, so one would shear the row it is painted into.
    pending_segments = [
        (sanitise_display_text(seg), style)
        for seg, style in (_trailing_segments(item, app) if not breadcrumb else [])
    ]
    label_to_render = sanitise_display_text(item.label)
    if width is not None and pending_segments:
        affordance_len = sum(
            len(seg_text) for seg_text, seg_style in pending_segments if "dim" not in seg_style
        )
        # Capped, so an over-long value elides itself rather than eating the
        # label's budget and eliding the label.
        affordance_len = min(affordance_len, max(8, width // 2))
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
        segments = _truncate_segments_to_fit(
            segments, budget=width - used - min_pad - gap, elide=item.elide
        )
        plain_len = sum(len(seg_text) for seg_text, _ in segments)
        pad = max(min_pad, width - used - plain_len - gap)
        text.append(" " + "·" * pad + " ", style="dim")
    else:
        text.append("   ")
    for seg_text, seg_style in segments:
        text.append(seg_text, style=seg_style)
    return text


def _shorten(text: str, keep: int, elide: str) -> str:
    """``text`` in ``keep`` cells, marking the end that was dropped."""
    if keep <= 1:
        return "…"
    return "…" + text[-(keep - 1) :] if elide == "head" else text[: keep - 1] + "…"


def _truncate_segments_to_fit(
    segments: list[tuple[str, str]], *, budget: int, elide: str = "tail"
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
    if reserved > budget:
        # A value is not an affordance: reserved in full, a long one runs past
        # the right border and the terminal cuts it unmarked. Glyphs are one or
        # two cells, so shrinking the longest segment leaves them whole.
        kept = [(t, s) for t, s in segments if "dim" not in s]
        longest = max(range(len(kept)), key=lambda i: len(kept[i][0]))
        room = budget - (reserved - len(kept[longest][0]))
        text, style = kept[longest]
        kept[longest] = (_shorten(text, room, elide), style)
        return kept
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
                summary = drill_summary(app, item.value_getter(app) or "")
            except Exception:
                summary = ""
        if summary:
            return [(summary + " ", "dim"), (_GLYPH_DRILL, "bold cyan")]
        return [(_GLYPH_DRILL, "bold cyan")]

    if item.kind == KIND_EXTERNAL:
        summary = ""
        if item.value_getter is not None:
            try:
                raw = item.value_getter(app) or ""
                # An external-app row's summary is the path it opens, not a
                # drill summary, so the mode does not govern it.
                summary = raw if item.external_app else drill_summary(app, raw)
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
        # A count is the fallback, not the rule: "40 selected" is the ABSENCE
        # of a type restriction, and "2 selected" never names the globs. A row
        # that can say what it holds says it.
        if item.value_getter is not None:
            try:
                value_str = str(item.value_getter(app))
            except Exception:
                value_str = "(unset)"
        elif isinstance(v, list):
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
    label_part = f" {sanitise_display_text(item.label)} "
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


def _coercion_error(coerce: Any, hint: str, err: Exception) -> str:
    """What a rejected value says back.

    `int` and `float` raise about themselves: "invalid literal for int() with
    base 10" names the coercion function, not the field. The row already
    carries the range it wants, so say that instead. Anything else raises for
    its own reasons and keeps its message.
    """
    if coerce is int:
        want = "a whole number"
    elif coerce is float:
        want = "a number"
    else:
        return f"invalid: {err}"
    return f"needs {want}" + (f" ({hint})" if hint else "")


def _out_of_bounds(item: MenuItem, value: Any) -> str:
    """Why the row refuses ``value``, or "".

    Nine rows printed a range in two places and enforced it in none, so
    `result_limit = 99999` against `1-1000` was written without a word.
    """
    bounds = getattr(item, "bounds", None)
    if bounds is None or not isinstance(value, int | float) or isinstance(value, bool):
        return ""
    low, high = bounds
    if low <= value <= high:
        return ""
    return f"outside {item.hint or f'{low}-{high}'}"


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
    /* Capped, or on a narrow terminal the label pushes the field off-screen and
       typing edits a value nobody can see. A width cap alone clips it: measured
       at 100 cols the Static wrapped to 4 rows inside a 2-row bar.
       `text-overflow` only elides an unwrapped line. */
    EditBar > Static.-edit-label {
        color: $text-muted; width: auto; max-width: 30%;
        text-wrap: nowrap; text-overflow: ellipsis;
    }
    /* Its own field, and capped after the name: eliding one string cut the
       range out of six rows of nine and rendered a seventh as `1…`, which
       reads as a different range rather than as a truncated one. */
    EditBar > Static.-edit-hint {
        color: $text-muted; width: auto; max-width: 40%;
        text-wrap: nowrap; text-overflow: ellipsis;
    }
    EditBar > Input#editor_input {
        border: none; padding: 0 1; color: $primary; background: $surface;
        width: 1fr; min-width: 12;
    }
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
        yield Static("", classes="-edit-hint")
        yield Input(id="editor_input", placeholder="")
        yield Static("", classes="-edit-error")

    def open(self, item: MenuItem, current_value: str) -> None:
        self._item = item
        self.query_one(".-edit-error", Static).update("")
        self.query_one(".-edit-label", Static).update(Text(f"Edit {item.label}", style="dim"))
        hint = f" · {item.hint} " if item.hint else " "
        self.query_one(".-edit-hint", Static).update(Text(hint, style="dim"))
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
        if path_is_absent(p):
            self._set_status("✗ does not exist", tone="error")
            return
        try:
            is_dir = p.is_dir()
        except OSError:
            self._set_status("⚠ unreadable", tone="warn")
            return
        if not is_dir:
            self._set_status("⚠ not a directory", tone="warn")
            return
        try:
            n = 0
            for _ in p.iterdir():
                n += 1
                if n >= self._PATH_ENTRY_CAP:
                    self._set_status(f"✓ folder: {self._PATH_ENTRY_CAP}+ items", tone="ok")
                    return
        except OSError:
            self._set_status("⚠ unreadable", tone="warn")
            return
        # A bare count beside a green tick reads as "N will be indexed", but it
        # is a non-recursive `iterdir` counting subfolders and `no_index` files;
        # `folder:` first survives a clip, and a gated walk would not survive the debounce.
        self._set_status(f"✓ folder: {n} items", tone="ok")

    @on(Input.Submitted, "#editor_input")
    def _on_submit(self, ev: Input.Submitted) -> None:
        if self._item is None:
            return
        raw = ev.value.strip()
        coerce = self._item.coerce or str
        try:
            # Empty input goes through coerce too: it is how an optional
            # setting is cleared and how a list row empties itself. Skipping
            # it posted the literal "" and validation rejected the write.
            value: Any = coerce(raw)
        except (TypeError, ValueError) as e:
            self.show_error(_coercion_error(coerce, self._item.hint, e))
            return
        out_of_range = _out_of_bounds(self._item, value)
        if out_of_range:
            self.show_error(out_of_range)
            return
        self.post_message(self.EditCommitted(self._item, value))

    @property
    def is_open(self) -> bool:
        return "-hidden" not in self.classes

    def commit_pending(self) -> None:
        """Submit what is typed, as Enter would.

        `^S` bypassed the open bar entirely, so a value the user had just
        typed was dropped without a word while the form saved without it.
        """
        if self._item is None or not self.is_open:
            return
        field = self.query_one("#editor_input", Input)
        self._on_submit(Input.Submitted(field, field.value))

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
        # Shift+Enter = reveal in the file manager (file-capable rows only).
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
        # One per item, in order. A query of the body also returns rows a
        # rebuild removed that Textual has not yet pruned; filling those leaves
        # the live rows blank.
        self._rows: list[Static] = []

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
        self._rows = []
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
            row = Static("", classes=cls)
            current_container.mount(row)
            self._rows.append(row)
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
        width = self.size.width or 80
        rows = self._rows
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
        rows = self._rows
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
        rows = self._rows
        if 0 <= self.cursor_index < len(rows):
            with contextlib.suppress(Exception):
                self.scroll_to_widget(rows[self.cursor_index], animate=False)

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
        """1-9 jumps to the Nth selectable item (skipping headers) and opens it.

        Opening is deliberate: it is the accelerator, not a side effect, so a
        digit never merely moves the cursor.
        """
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
        """Shift+Enter on a reveal-capable row shows the underlying file in the
        platform file manager. Capability is keyed off well-known row ids."""
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

    # ── Layout ──────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        title = (
            "Settings & Commands"
            if not self._breadcrumb
            else f"Settings & Commands › {' › '.join(self._breadcrumb)}"
        )
        with Vertical(id="settings_box") as box:
            box.border_title = title
            # Naming the key, as the filter browser's box does: these screens
            # can open with the LIST focused, where a typed letter runs its
            # command: `q` on the Keybindings sheet quits the app.
            yield Input(placeholder=_SEARCH_PLACEHOLDER_WITH_KEY, id="settings_search")
            yield SettingsList()
            yield DetailStrip()
            if not self._breadcrumb:
                # Root-only version + build identifier; sub-screens omit it.
                yield Static("", id="settings_status")
            # Inside the panel: this one is inset, so a screen-docked bar
            # painted at column 0, detached from the row it was editing.
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
        """Refresh items when control returns from a popped child screen."""
        self.refresh_items()

    def refresh_items(self) -> None:
        """Re-run the provider and repaint every row's trailing value.

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

        # Every cached trailing value, not a hand-maintained list that drifts:
        # a screen resuming or a run finishing is exactly the moment none of
        # them can be trusted.
        from fnd.tui.lazy_trailing import invalidate_all

        invalidate_all()

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
        # A repaint that lands mid-search must not widen the list back out:
        # the box still holds the query, so the rows have to keep matching it.
        if self._filter_active:
            typed = self.query_one("#settings_search", Input).value
            if typed.strip():
                self._apply_filter(typed, cursor_id=prev_id)
                self._refresh_hint_bar()
                return
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
        # `/`, `:`, `?` and `q` type into a focused box rather than acting, so
        # naming them there advertises four keys that do not work.
        bar = _editor_hint_bar(cluster) if self._is_typing() else _hint_bar(app, cluster)
        self.query_one("#footer_hints", Static).update(bar)

    def _is_typing(self) -> bool:
        """Whether a text box has focus, so the anchors are inert."""
        import contextlib

        with contextlib.suppress(Exception):
            if "-hidden" not in self.query_one(EditBar).classes:
                return True
        with contextlib.suppress(Exception):
            return self.query_one("#settings_search", Input).has_focus
        return False

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
            return (("↓", "Results"), ("⏎", "Go to first"), ("Esc", "Clear"))

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
        self._apply_filter(ev.value)

    def _apply_filter(self, raw: str, *, cursor_id: str | None = None) -> None:
        """Show the rows matching ``raw``, or the whole list when it is empty.

        One implementation for typing and for a repaint: a second copy in
        `refresh_items` kept the rows and dropped the breadcrumbs, which turns
        a flat result list back into a bordered menu, and destroyed the
        no-matches placeholder rather than keeping it.
        """
        q = raw.strip().lower()
        lst = self.query_one(SettingsList)
        lst._search_query = q
        if not q:
            self._filter_active = False
            lst.set_items(list(self._items), cursor_id=cursor_id)
            return
        self._filter_active = True
        filtered, breadcrumbs = self._filter_items(q)
        if not filtered:
            # Empty-state hint — a non-selectable placeholder row so the
            # cursor-skip rule keeps it inert.
            placeholder = MenuItem(
                id="search.empty",
                label=(f"No matches for '{raw.strip()}'. Try shorter terms or press Esc to clear."),
                kind=KIND_HEADER,
            )
            lst.set_items([placeholder])
            return
        lst.set_items(filtered, breadcrumbs=breadcrumbs, cursor_id=cursor_id)

    def _filter_items(self, q: str) -> tuple[list[MenuItem], dict[int, tuple[str, ...]]]:
        """Cross-section: walk every section's leaves, score by substring
        match against label + key + keywords + breadcrumb segments.

        Spec deliberately excludes ``description`` prose — descriptions
        surface in the detail strip on focus, and indexing them muddies
        the search results.
        """
        matches: list[tuple[int, int, MenuItem, tuple[str, ...]]] = []
        app: FNDApp = self.app  # type: ignore[assignment]
        seen: set[str] = set()
        # This page's own rows first: `walk_all_sections` does not descend
        # per-collection sub-screens, so without them the filter would search
        # everywhere EXCEPT the page in front of you.
        here: list[tuple[tuple[str, ...], MenuItem]] = [
            (self._breadcrumb, it) for it in self._items
        ]
        for local, source in ((0, iter(here)), (1, walk_all_sections(app))):
            for path, item in source:
                if item.kind == KIND_HEADER or item.id in seen:
                    continue
                seen.add(item.id)
                haystack = " ".join((item.label, item.key, *item.keywords, *path)).lower()
                idx = haystack.find(q)
                if idx == -1:
                    continue
                # Earlier match in the label scores higher (smaller idx first).
                label_idx = item.label.lower().find(q)
                score = label_idx if label_idx != -1 else 1000 + idx
                matches.append((local, score, item, path))
        matches.sort(key=lambda m: (m[0], m[1], len(m[2].label)))
        breadcrumbs = {id(item): path for _, _, item, path in matches}
        return [item for _, _, item, _ in matches], breadcrumbs

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
        refresh_search_placeholder(self, "#settings_search")

    def on_descendant_blur(self, _ev: events.DescendantBlur) -> None:
        self._refresh_hint_bar()
        refresh_search_placeholder(self, "#settings_search")

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
                    self.notify(_summarise(e), severity="error")
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
            if item.scalar_setter is not None:
                item.scalar_setter(app, ev.value)
            elif item.setting_path:
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
            self.query_one(EditBar).show_error(_summarise(e))
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
            # Nor `/`: invoking it here closes the sheet and focuses the query
            # bar, leaving this screen's own filter box unreachable while its
            # placeholder invites typing and every letter runs a command, `q` included.
            if item.action_id == "focus_query":
                continue
            # Rows documenting another screen's widget keys carry no action, so
            # "invoking" one would close the whole settings stack and do nothing.
            if not item.action_id:
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


def _summarise(exc: Exception) -> str:
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
    Enter toggles ``✓``, ``^S`` commits, Esc cancels.
    """

    BINDINGS = [  # noqa: RUF012
        Binding("escape,left", "back", "Back", show=False),
        Binding("ctrl+s", "save_close", show=False),
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
        options = self.query_one("#picker_list", OptionList)
        options.focus()
        if options.option_count and options.highlighted is None:
            # On the current value: unhighlighted, ⏎ is a dead key; on option 0,
            # a single-select ⏎ changes the setting the user only opened to read.
            options.highlighted = next(
                (i for i, c in enumerate(self._choices) if c.value in self._selected), 0
            )
        self._render_footer()

    def _render_footer(self) -> None:
        app: FNDApp = self.app  # type: ignore[assignment]
        hints: tuple[tuple[str, str], ...] = (
            (("⏎", "Toggle"), (COMMIT_KEY, "Save"), ("Esc", "Cancel"))
            if self._item.multi
            else (("⏎", "Select"), ("Esc", "Cancel"))
        )
        bar = _editor_hint_bar(hints) if _typing_in(self) else _hint_bar(app, hints, screen=self)
        self.query_one("#footer_hints", Static).update(bar)

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
        """Esc cancels, on a multi picker too, as on the single-select row.

        `^S` saves, as on every other screen that edits something.
        """
        self.app.pop_screen()

    def action_save_close(self) -> None:
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
            self.notify(_summarise(e), severity="error", title="Save failed")


def _same_setting(value: Any, default: Any) -> bool:
    """Whether a value differs from the default enough to be an override.

    ``None``, ``""`` and ``[]`` all mean "no value here", so an untouched
    field must not be recorded: doing so would turn inheriting into an
    explicit empty and silently drop the default it was inheriting.
    """
    empty = (None, "", [], {})
    if value in empty and default in empty:
        return True
    return bool(value == default)


def open_source_filter_browser(
    app: FNDApp,
    overrides: dict[str, Any],
    root: Any,
    on_change: Callable[[], None],
    globs: list[str] | None = None,
    excludes: list[str] | None = None,
) -> None:
    """The source's *effective* filters, edited as branches.

    Showing the resolved set and recording only what differs from the defaults
    means there is no third "inherit" state to explain, and no ``-`` sentinel:
    change something and it becomes an override, put it back and it stops
    being one.
    """
    import dataclasses

    from fnd.config import DefaultFilters, SourceFilters, resolve_filters
    from fnd.filters import build_gate, spec_from_resolved
    from fnd.filters.scan import sample_source

    cfg = app._config  # type: ignore[attr-defined]
    defaults = cfg.defaults.filters if cfg else DefaultFilters()
    resolved = resolve_filters(SourceFilters.model_validate(overrides or {}), defaults)

    def _sample(spec: Any = None) -> Any:
        if root is None or path_is_absent(root):
            return None
        # The source's own ignore settings, so the offered types and tags are
        # the ones this source would actually index.
        names = tuple(
            name
            for name, on in (
                (".gitignore", resolved.respect_gitignore),
                (".fndignore", resolved.respect_fndignore),
            )
            if on
        )
        # The rules the screen is SHOWING, not the ones it opened with, minus
        # the kind rule: a kind the user has not ticked would otherwise read
        # `· 0` and tell them nothing about what ticking it would bring in.
        gating = spec if spec is not None else spec_from_resolved(resolved)
        return sample_source(
            root,
            budget_s=0.8,
            ignore_names=names,
            # The pane names these in its own footer as paths it is skipping.
            excludes=list(excludes or ()),
            gate=build_gate(dataclasses.replace(gating, kinds=())),
        )

    def _save(spec: Any, gitignore: bool, fndignore: bool) -> None:
        values = _spec_to_mapping(spec)
        values["respect_gitignore"] = gitignore
        values["respect_fndignore"] = fndignore
        # The tree does not edit `clears`, and this rebuilds the overrides from
        # scratch, so anything it cannot express has to be carried across or it
        # is deleted by visiting the screen.
        carried = {k: v for k, v in overrides.items() if k not in values and v}
        overrides.clear()
        overrides.update(carried)
        for name, value in values.items():
            if not _same_setting(value, getattr(defaults, name, None)):
                overrides[name] = value
        on_change()

    app.push_screen(
        FilterBrowserScreen(
            title="Index filters · this source",
            spec=_spec_from_filters(resolved),
            gitignore=resolved.respect_gitignore,
            fndignore=resolved.respect_fndignore,
            sample_provider=_sample,
            no_tags_note="no tags found in this source",
            globs=list(globs or ()),
            excludes=list(excludes or ()),
            inherited=(
                _spec_from_filters(defaults),
                defaults.respect_gitignore,
                defaults.respect_fndignore,
            ),
            save_note=(
                f"{COMMIT_KEY} applies here; {COMMIT_KEY} on the source form saves and reindexes"
            ),
            commit_label="Apply",
            on_save=_save,
        )
    )


def _default_filters(app: Any) -> Any:
    """The global default filters, or the shipped ones when no config."""
    from fnd.config import DefaultFilters

    cfg = getattr(app, "_config", None)
    return getattr(getattr(cfg, "defaults", None), "filters", None) or DefaultFilters()


def _default_frontmatter(app: Any) -> str:
    cfg = getattr(app, "_config", None)
    return (getattr(cfg.defaults.filters, "frontmatter", None) or "") if cfg else ""


def _seeded_filters(source: Any) -> dict[str, Any]:
    """A source's filter overrides, with a legacy rule folded in.

    ``frontmatter_filter`` predates ``filters.frontmatter``. Seeding it here
    means the browser, the only surface for the rule, shows it, and clearing
    it there actually clears it.
    """
    values: dict[str, Any] = source.filters.model_dump(exclude_none=True) if source.filters else {}
    # `clears` defaults to a list, so exclude_none always carries it. An empty
    # one is not an override, and leaving it in makes every open-and-save look
    # like a change and force a rebuild.
    if not values.get("clears"):
        values.pop("clears", None)
    legacy = str(source.legacy_frontmatter or "")
    if legacy and not values.get("frontmatter"):
        values["frontmatter"] = legacy
    return values


def _source_frontmatter(source: Any) -> str:
    """This source's frontmatter rule, wherever it is currently stored."""
    override = getattr(source.filters, "frontmatter", None) if source.filters else None
    if override is not None:
        return str(override)
    return str(source.legacy_frontmatter or "")


def _merge_frontmatter(
    filters: dict[str, Any], text: str, default: str, *, had_override: bool = True
) -> dict[str, Any]:
    """Fold the rule into ``filters``, keeping "same as the default" unset.

    Emptying a rule the source owned is a real override to nothing, and
    dropping the key there would reinstate the inherited rule. A source that
    never had one renders the same empty field, so without ``had_override``
    opening the form and saving unchanged converted "inherit" into "no rule"
    and, because nothing else differed, fired no reindex to reveal it.
    """
    value = text.strip()
    if value == default.strip() or (not value and not had_override):
        filters.pop("frontmatter", None)
    else:
        filters["frontmatter"] = value
    return filters


def _source_filters_or_none(raw: dict[str, Any] | None) -> Any:
    """Sparse overrides as a ``SourceFilters``, or ``None`` when none are set.

    Only ``None`` means "inherit". An empty list is the explicit override to
    nothing (the row's ``-``), so dropping it here would silently reinstate
    the global value the user was overriding.
    """
    from fnd.config import CLEARABLE, SourceFilters

    items = raw or {}
    cleaned = {k: v for k, v in items.items() if v is not None}
    # A number or date set to None is the user choosing "no limit here" over an
    # inherited one; dropped, it would revert on the next open. A bool or a
    # list is not clearable: false and [] already say it.
    cleared = sorted(k for k, v in items.items() if v is None and k in CLEARABLE)
    if cleared:
        cleaned["clears"] = cleared
    return SourceFilters.model_validate(cleaned) if cleaned else None


def _exclude_globs(fields: dict[str, Any]) -> list[str]:
    """Every exclude glob in force, presets expanded."""
    from fnd.config import EXCLUDES_PRESETS

    out: list[str] = []
    for key in fields.get("excludes_presets") or ():
        if key in EXCLUDES_PRESETS:
            out.extend(EXCLUDES_PRESETS[key]["globs"])
    out += [g.strip() for g in str(fields.get("excludes_custom") or "").split(",") if g.strip()]
    return out


def _excludes_summary(fields: dict[str, Any]) -> str:
    """The presets and globs by name: a count would show the globs nowhere in
    the UI."""
    from fnd.config import EXCLUDES_PRESETS

    named = [
        str(EXCLUDES_PRESETS[key]["label"])
        for key in fields.get("excludes_presets") or ()
        if key in EXCLUDES_PRESETS
    ]
    named += [g.strip() for g in str(fields.get("excludes_custom") or "").split(",") if g.strip()]
    return ", ".join(named) if named else "(none)"


def _overridden_fields(overrides: dict[str, Any] | None) -> list[str]:
    """The settings a source overrides, one name each.

    ``clears`` is one key naming any number of fields, so counting keys
    reports three cleared bounds as one override.
    """
    names = {k for k, v in (overrides or {}).items() if k != "clears" and v is not None}
    names.update((overrides or {}).get("clears") or ())
    return sorted(names)


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
    cascades, and repaints exactly like the file-type filter. Changes apply as
    they are toggled, so leaving is all there is to do.

    That is the opposite of the filter browser, which holds its edits because
    saving one reindexes. Both are right for what they edit; what was wrong is
    that the gesture a user learnt on one did nothing on the other."""

    BINDINGS = [  # noqa: RUF012
        Binding("escape,left", "back", "Back", show=False),
        Binding("ctrl+s", "back", "Done", show=False),
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
            _hint_bar(
                app,
                (
                    ("⏎", "Toggle"),
                    ("←/→", "Collapse/Expand"),
                    (f"Esc/{COMMIT_KEY}", "Done"),
                ),
            )
        )

    @on(ToggleTree.SelectionChanged)
    def _on_changed(self, ev: ToggleTree.SelectionChanged) -> None:
        # Commit live so the row summary updates as the user toggles.
        self._commit(ev.selected)

    @on(ToggleTree.NavigatedOut)
    def _on_navigated_out(self, _ev: ToggleTree.NavigatedOut) -> None:
        """← at the outermost level leaves, as it does everywhere else in
        Settings. The tree's own binding would otherwise swallow it."""
        self.action_back()

    def action_back(self) -> None:
        self._commit(self.query_one("#tree_picker", ToggleTree).selected)
        self.app.pop_screen()

    def _commit(self, values: frozenset[str]) -> None:
        if self._item.picker_setter is None:
            return
        try:
            self._item.picker_setter(self.app, sorted(values))  # type: ignore[arg-type]
        except Exception as e:
            self.notify(_summarise(e), severity="error", title="Save failed")


# ── Collection-form screens (rebuilt from CollectionsScreen) ────────


def _kinds_to_include_globs(kind_ids: list[str]) -> list[str]:
    """Expand selected kind ids to include globs for all their suffixes."""
    from fnd.kinds import KIND_BY_ID

    globs: list[str] = []
    for kid in kind_ids:
        spec = KIND_BY_ID.get(kid)
        if spec is not None:
            globs.extend(f"**/*{sfx}" for sfx in spec.suffixes)
    return globs


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
        # Globs an untick cleared, so re-ticking can offer them back.
        self._discarded_globs: dict[str, str] = {}
        self._fields: dict[str, Any] = {
            "path": "",
            "includes_custom": "",  # comma-separated free-form globs
            "excludes_presets": [],  # list[str] of EXCLUDES_PRESETS keys
            "excludes_custom": "",  # comma-separated free-form globs
            "filter": "",
            "follow_symlinks": False,
            # Per-source app override + Obsidian vault.
            # ``app`` is the registry id (or "" = no override → resolver
            # walks the global app_defaults / auto-promote ladder).
            # ``app_params_vault`` is the only app_param that has UI
            # surface today; other params still reachable via the TOML.
            "app": "",
            "app_params_vault": "",
            # Sparse SourceFilters overrides; empty means "inherit everything".
            "filters": {},
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
            yield Static("", id="form_sample_sep", classes="form_separator")
            yield TextArea("", id="frontmatter_sample")
            yield Static("(no sample)", id="match_status")
            yield Static("", id="form_error", classes="-hidden")
            yield DetailStrip()
        yield EditBar()
        yield Static("", id="footer_hints")

    @on(SettingsList.Highlighted)
    def _on_field_highlighted(self, ev: SettingsList.Highlighted) -> None:
        """Show the highlighted row's description, as the wizard editing the
        same fields does."""
        strip = self.query_one(DetailStrip)
        item = ev.item
        if item is None:
            strip.clear()
            return
        strip.set(item.description or "", item.hint or "", markup=item.description_markup)

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
        # Every include glob, type globs too: the model folds those into
        # `kinds` only when nothing else is listed, so one still here is an
        # ORed path glob like its neighbours, not a file type.
        preset_keys, excludes_custom = _split_excludes_globs(list(s.excludes))
        self._fields = {
            "path": str(s.path),
            "includes_custom": ", ".join(s.includes),
            "excludes_presets": preset_keys,
            "excludes_custom": excludes_custom,
            "filter": _source_frontmatter(s),
            "follow_symlinks": bool(s.follow_symlinks),
            "app": s.app or "",
            "app_params_vault": (s.app_params or {}).get("vault", ""),
            "filters": _seeded_filters(s),
        }
        # Copied wholesale rather than key by key: a snapshot missing a field
        # never equals the fields, so every save would force a rebuild.
        self._snapshot = copy.deepcopy(self._fields)

    def _frontmatter_text(self) -> str:
        """This source's frontmatter rule.

        Only the overrides: a legacy ``frontmatter_filter`` is folded into
        them at load, so falling back to it here would resurrect a rule the
        user has just cleared in the browser.
        """
        return str(self._fields["filters"].get("frontmatter") or "")

    def _effective_frontmatter(self) -> tuple[str, bool]:
        """The rule that actually applies, and whether it came from the
        defaults. A source with no override of its own still has files dropped
        by `[defaults.filters].frontmatter`, and testing a sample against
        nothing told the user the opposite."""
        own = self._frontmatter_text().strip()
        if own:
            return own, False
        cfg = getattr(self.app, "_config", None)
        inherited = getattr(getattr(cfg, "defaults", None), "filters", None)
        return str(getattr(inherited, "frontmatter", None) or "").strip(), True

    def _frontmatter_into_filters(self, text: str) -> dict[str, Any]:
        """The frontmatter rule as part of this source's filter overrides.

        As a separate field beside the filters, the browser and the row could
        disagree about the same rule with neither showing the other's value.
        """
        return _merge_frontmatter(
            dict(self._fields["filters"]),
            text,
            _default_frontmatter(self.app),
            had_override=self._fields["filters"].get("frontmatter") is not None,
        )

    def _open_filters(self) -> None:
        app: FNDApp = self.app  # type: ignore[assignment]
        raw_path = str(self._fields.get("path") or "").strip()
        root = Path(raw_path).expanduser() if raw_path else None
        globs = [
            g.strip()
            for g in str(self._fields.get("includes_custom") or "").split(",")
            if g.strip()
        ]
        open_source_filter_browser(
            app,
            self._fields["filters"],
            root,
            self._populate_fields,
            globs,
            _exclude_globs(self._fields),
        )

    def _filters_summary(self) -> str:
        count = len(_overridden_fields(self._fields.get("filters")))
        return f"{count} overridden" if count else "inherited"

    def _populate_fields(self) -> None:
        self.query_one(SettingsList).set_items(self._build_field_items())
        self._refresh_sample_tester()
        # A rejected save left its reason on screen while the user fixed the
        # very field it named, so "Name is required." sat above a filled name.
        self._clear_error()

    def _refresh_sample_tester(self) -> None:
        """The tester only appears once there is a rule for it to test.

        It cost a third of the form on every source, and named a rule that
        lives two screens away without saying which.
        """
        rule, inherited = self._effective_frontmatter()
        for wid in ("#form_sample_sep", "#frontmatter_sample", "#match_status"):
            self.query_one(wid).display = bool(rule)
        if not rule:
            return
        source = " (inherited)" if inherited else ""
        shown = sanitise_display_text(rule)
        width = max(20, self.size.width - 12)
        if len(shown) > width:
            shown = shown[: width - 1] + "…"
        self.query_one("#form_sample_sep", Static).update(
            f"─── Paste frontmatter to test:  {shown}{source} ───"
        )

    def _build_field_items(self) -> list[MenuItem]:
        from fnd.config import EXCLUDES_PRESETS

        return [
            header("Source", level=2),
            self._field_item(
                "path",
                "Path",
                hint="path or ~/path",
                description=(
                    "The folder to index. ~ expands; the path must exist. "
                    "Changing it reindexes this source from scratch."
                ),
            ),
            MenuItem(
                id="form.follow_symlinks",
                label="Follow symlinks",
                description=(
                    "Descend into symlinked folders. Off by default: a link "
                    "pointing back up the tree would index the same files "
                    "repeatedly."
                ),
                kind=KIND_TOGGLE,
                toggle_getter=lambda _app: bool(self._fields["follow_symlinks"]),
                toggle_setter=lambda _app, v: self._set_follow(v),
            ),
            header("What gets indexed", level=2),
            MenuItem(
                id="form.filters",
                label="Index filters",
                description=(
                    "Ignore files, skipped tags, file types and size for this "
                    "source. Each setting inherits the global default until you "
                    "override it here."
                ),
                kind=KIND_EXTERNAL,
                external=lambda _app: self._open_filters(),
                value_getter=lambda _app: self._filters_summary(),
            ),
            self._field_item(
                "includes_custom",
                "Restrict to these paths",
                hint="glob patterns, comma-separated",
                description=(
                    "Leave empty to index the whole folder. Set it and ONLY "
                    "matching paths are indexed: these globs replace the "
                    "default, they do not add to it, so 'notes/**' alone "
                    "means notes/ and nothing else. To keep everything and "
                    "add a hidden folder, name both: "
                    "'**/*.md, .obsidian/**'. File types belong in Index "
                    "filters."
                ),
            ),
            MenuItem(
                id="form.excludes",
                label="Excludes",
                value_getter=lambda _app: _excludes_summary(self._fields),
                description=(
                    "Paths to skip, as ready-made presets or your own globs. "
                    "Applied before any filter, so an excluded folder is never "
                    "read at all."
                ),
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
            header("Opening", level=2),
            MenuItem(
                id="form.app",
                label="App",
                description=(
                    "Open files from this source with a specific app. "
                    "Leave it unset to use the global default and the "
                    "auto-promote ladder, which [app_defaults] in config.toml "
                    "sets. docs/apps.md lists every app and how to add one."
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
            # `reveal` never opens the file, so it can't stand in as a
            # source's app — see ``App.selectable_default``.
            if not app.selectable_default:
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

    def _excludes_picker_state(self) -> list[str]:
        state = list(self._fields["excludes_presets"])
        if str(self._fields.get("excludes_custom") or "").strip():
            state.append("__custom__")
        return state

    def _set_excludes(self, values: list[str]) -> None:
        picked = list(values)
        wants_custom = "__custom__" in picked
        self._fields["excludes_presets"] = [v for v in picked if v != "__custom__"]
        if wants_custom and not str(self._fields.get("excludes_custom") or "").strip():
            self._prompt_custom("excludes_custom", "Excludes custom globs (comma-separated)")
        elif not wants_custom:
            self._discard_custom("excludes_custom")
        self.query_one(SettingsList).refresh_values()

    def _discard_custom(self, field_key: str) -> None:
        """Untick clears the globs, but keeps them for the visit.

        The tick is derived from the text, so leaving it set would re-tick the
        row; dropping it outright would lose typed globs to one keypress.
        """
        _discard_custom_globs(self, field_key)

    def _prompt_custom(self, field_key: str, label: str) -> None:
        item = MenuItem(
            id=f"form.{field_key}",
            label=label,
            hint=_GLOB_HINT,
            kind=KIND_SCALAR,
            value_getter=lambda _app, key=field_key: str(self._fields.get(key) or ""),
        )
        self.query_one(EditBar).open(item, _custom_seed(self, field_key))

    def _field_item(self, key: str, label: str, *, hint: str, description: str = "") -> MenuItem:
        def _get(_app: Any) -> str:
            v = self._fields[key]
            if key == "filter" and v:
                status = self._parse_status(v)
                return f"{v}   {status}".rstrip()
            if key == "path" and v:
                return _display_path(v)
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
            elide="head" if key == "path" else "tail",
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
            self.query_one(EditBar).open(item, str(current or ""))
        elif item.kind == KIND_TOGGLE:
            new = not (item.toggle_getter(self.app) if item.toggle_getter else False)  # type: ignore[arg-type]
            if item.toggle_setter is not None:
                item.toggle_setter(self.app, new)  # type: ignore[arg-type]
            self.query_one(SettingsList).refresh_values()
        elif item.kind == KIND_EXTERNAL and item.external is not None:
            item.external(self.app)  # type: ignore[arg-type]

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
        filter_text, inherited = self._effective_frontmatter()
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
            status.update("(no rule, here or in the defaults)")
            return
        source = " (inherited from the defaults)" if inherited else ""
        pred, err = parse_or_error(filter_text)
        if err is not None or pred is None:
            status.update(f"✗ filter syntax: col {err.column}" if err else "✗ syntax error")
            status.add_class("-no-match")
            return
        if pred(fm):
            status.update(f"✓ sample matches the rule{source}")
            status.add_class("-match")
        else:
            status.update(f"✗ sample does not match the rule{source}")
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

    def _still_the_same_source(self, col: Any) -> bool:
        """Whether this form's row index still names the source it opened on.

        `_snapshot` holds the fields as loaded, so its path is the one the user
        started editing whatever they have since typed into the field.
        """
        index = self._source_index
        if index is None or index >= len(col.sources):
            return False
        opened = str(self._snapshot.get("path") or "")
        if not opened:
            # No snapshot means nothing to compare, which is not the same as
            # agreement: three early returns leave `_snapshot` empty.
            return False
        return str(col.sources[index].path) == opened

    def _render_footer(self) -> None:
        app: FNDApp = self.app  # type: ignore[assignment]
        # Ctrl+D only meaningful when editing an existing source.
        # Tab is only named while there is a second pane to reach: the sample
        # tester is hidden without a rule to test.
        hints: tuple[tuple[str, str], ...] = (
            *((("Tab", "Test a sample"),) if len(_focus_targets(self)) > 1 else ()),
            ("⏎", "Edit"),
            (COMMIT_KEY, "Save"),
            ("Esc", "Cancel"),
        )
        if self._source_index is not None:
            hints = (*hints, ("Ctrl+D", "Delete source"))
        bar = _editor_hint_bar(hints) if _typing_in(self) else _hint_bar(app, hints, screen=self)
        self.query_one("#footer_hints", Static).update(bar)

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
                source_path=str(self._snapshot.get("path") or ""),
            )
        )

    def action_save_close(self) -> None:
        # An open edit bar holds a value the user has typed but not submitted;
        # saving over the top of it dropped that value silently.
        if _commit_then(self, self.action_save_close):
            return
        from pathlib import Path

        from fnd.config import (
            EXCLUDES_PRESETS,
            CollectionConfig,
            SourceConfig,
            default_config_path,
            load,
            overlapping_source,
            write_collection,
        )

        self._clear_error()

        path = str(self._fields["path"] or "").strip().strip("'\"")
        if blocked := self.save_blocked():
            self._show_error(blocked)
            return
        includes_globs: list[str] = []
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
        app: FNDApp = self.app  # type: ignore[assignment]
        # Read the file, not the launch snapshot: `write_collection` replaces the
        # collection table wholesale, so a stale model deletes sources added by
        # hand. No fallback to `app._config`: the guard below would compare it to itself.
        try:
            cfg = load()
        except Exception as e:
            self._show_error(
                f"The config on disk cannot be read, so this save would overwrite it: "
                f"{_summarise(e)}"
            )
            return
        if self._collection_name not in cfg.collections:
            self._show_error("Collection vanished. Please reopen the menu.")
            return
        col: CollectionConfig = cfg.collections[self._collection_name]
        # A fresh read makes the row index mean whatever now sits there, so an
        # edit could land on a different source. Refuse rather than guess.
        if self._source_index is not None and not self._still_the_same_source(col):
            # Adopt the fresh read even though the write is refused: the form
            # reseeds from `app._config`, so without this the "reopen it" advice
            # could never succeed.
            app._config = cfg  # type: ignore[attr-defined]
            self._show_error(
                "This row is not the source it was when the form opened: the "
                "config changed on disk. Press Esc and reopen it."
            )
            return
        app._config = cfg  # type: ignore[attr-defined]
        # Start from the source as it stands and overwrite only the fields this
        # form owns, keeping those it has no control for (app_for, and
        # app_params beyond vault).
        prior = col.sources[self._source_index] if self._source_index is not None else None
        values = dict(prior.model_dump(mode="python")) if prior else {}
        app_id = str(self._fields.get("app") or "").strip()
        vault = str(self._fields.get("app_params_vault") or "").strip()
        app_params: dict[str, str] = dict(values.get("app_params") or {})
        if vault:
            app_params["vault"] = vault
        else:
            app_params.pop("vault", None)
        values.update(
            path=Path(path),
            includes=includes_globs,
            excludes=excludes_globs,
            follow_symlinks=bool(self._fields["follow_symlinks"]),
            frontmatter_filter=None,
            filters=_source_filters_or_none(
                self._frontmatter_into_filters(self._frontmatter_text())
            ),
            app=app_id or None,
            app_params=app_params,
        )
        try:
            new_source = SourceConfig.model_validate(values)
        except Exception as e:
            self._show_error(_summarise(e))
            return

        overlap, overlap_contains = overlapping_source(col.sources, new_source, self._source_index)
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
            self._show_error(_summarise(e))
            return
        app._config = load()  # type: ignore[attr-defined]
        app._scope.refresh_collections_panel()  # type: ignore[attr-defined]
        if overlap:
            # Harmless (the index keys on the file, so a file reached twice is
            # stored once), but a source that indexes nothing new is worth
            # knowing about rather than discovering from a file count.
            app.notify(
                f"This folder {'already covers' if overlap_contains else 'is already inside'} "
                f"{overlap!r} in this collection; files reached by both are indexed once.",
                severity="warning",
            )
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

    def unsaved_work(self) -> tuple[str, Callable[[], None]] | None:
        """What leaving now would lose, and how to keep it."""
        if self._snapshot == self._fields:
            return None
        return "this source", self.action_save_close

    def save_blocked(self) -> str:
        """Why ``^s`` would be refused, or "". The save and the leaving prompt
        read this same answer, so the prompt cannot offer a save that is
        certain to fail."""
        from pathlib import Path

        path = str(self._fields["path"] or "").strip().strip("'\"")
        if not path:
            return "Path is required."
        if path_is_absent(Path(path).expanduser()):
            return f"Path does not exist: {path}"
        return ""

    def action_back(self) -> None:
        # The filter browser saves into `_fields`, not to disk, so leaving the
        # form is what discards it, including an edit the user has just
        # committed one screen down.
        _leave_or_confirm(
            self,
            dirty=self._snapshot != self._fields,
            what="this source",
            on_save=self.action_save_close,
        )

    # ── Tab cycles field list ↔ sample TextArea ───────────────

    def action_cycle_focus(self, direction: int) -> None:
        widgets = _focus_targets(self)
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
        /* Fixed, not auto: the edit bar lives inside the panel so it travels
           with it, and an auto width jumped to max the moment it opened. */
        width: 76;
        max-width: 100%;
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

        # Globs an untick cleared, so re-ticking can offer them back.
        self._discarded_globs: dict[str, str] = {}
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
            yield Static("", id="form_sample_sep", classes="form_separator")
            yield TextArea("", id="frontmatter_sample")
            yield Static("(no sample)", id="match_status")
            yield Static("", id="wizard_error", classes="-hidden")
            yield DetailStrip()
            # Inside the panel: this one is centred with an auto width, so a
            # screen-docked bar painted at the far left, detached from the row
            # it was editing.
            yield EditBar()
        yield Static("", id="footer_hints")

    def _show_error(self, message: str) -> None:
        """Render an inline validation error in the wizard's #wizard_error
        Static. The old `notify()` toast pattern was dropped so the
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
        # An untouched form, to tell "nothing typed yet" from "a filled form
        # about to be thrown away".
        self._opened_with = copy.deepcopy(self._fields)
        app: FNDApp = self.app  # type: ignore[assignment]
        self.query_one("#footer_hints", Static).update(_wizard_hints(self, app))

    def _populate_fields(self) -> None:
        self.query_one(SettingsList).set_items(self._build_field_items())
        self._refresh_sample_tester()
        # A rejected save left its reason on screen while the user fixed the
        # very field it named, so "Name is required." sat above a filled name.
        self._clear_error()

    def _refresh_sample_tester(self) -> None:
        """As on the source form: nothing to test without a rule."""
        rule = str(self._fields.get("filter") or "").strip()
        for wid in ("#form_sample_sep", "#frontmatter_sample", "#match_status"):
            self.query_one(wid).display = bool(rule)
        if rule:
            self.query_one("#form_sample_sep", Static).update(
                f"─── Paste frontmatter to test:  {sanitise_display_text(rule)} ───"
            )

    def _build_field_items(self) -> list[MenuItem]:
        from fnd.config import EXCLUDES_PRESETS

        return [
            MenuItem(
                id="wiz.name",
                label="Name",
                description="What this collection is called in the sidebar and in `-c`.",
                kind=KIND_SCALAR,
                value_getter=lambda _app: self._fields["name"] or "(required)",
            ),
            MenuItem(
                id="wiz.path",
                label="Source path",
                description="The folder to index. Add more sources to it afterwards.",
                kind=KIND_SCALAR,
                value_getter=lambda _app: self._fields["path"] or "(required)",
                elide="head",
            ),
            MenuItem(
                id="wiz.includes",
                label="File types",
                value_getter=lambda _app: self._summarise_includes(),
                description=(
                    "Which types to index. Tick none for every supported type, "
                    "which also picks up ones added in later versions."
                ),
                kind=KIND_PICKER,
                multi=True,
                groups_provider=lambda _app: _includes_groups(),
                picker_getter=lambda _app: self._includes_picker_state(),
                picker_setter=lambda _app, vs: self._set_includes(vs),
            ),
            MenuItem(
                id="wiz.excludes",
                label="Excludes",
                value_getter=lambda _app: self._summarise_excludes(),
                description=(
                    "Paths to skip, as presets or your own globs. Applied "
                    "before any filter, so an excluded folder is never read."
                ),
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
                label="Frontmatter rule",
                description=(
                    "Index only notes whose YAML frontmatter matches, e.g. "
                    "status == 'done'. Files without frontmatter are unaffected."
                ),
                kind=KIND_SCALAR,
                hint="frontmatter DSL",
                value_getter=lambda _app: self._filter_with_status(),
            ),
            MenuItem(
                id="wiz.follow_symlinks",
                label="Follow symlinks",
                description="Index through symlinked folders. Off avoids indexing a tree twice.",
                kind=KIND_TOGGLE,
                toggle_getter=lambda _app: bool(self._fields["follow_symlinks"]),
                toggle_setter=lambda _app, v: self._set_follow(v),
            ),
        ]

    def _summarise_includes(self) -> str:
        """What the new collection will actually index.

        Setting nothing here does not mean "every type": the source inherits
        `defaults.filters`, so with a default of `kinds = ["md"]` "every type"
        would be false while the collection indexes 3 files of 12.
        """
        from fnd.kinds import ALL_KIND_IDS

        n = len(self._fields["includes"])
        if n == len(ALL_KIND_IDS):
            return "every type"
        if n:
            return f"{n} of {len(ALL_KIND_IDS)} types"
        inherited = list(_default_filters(self.app).kinds)
        if inherited:
            return f"{', '.join(inherited)} (inherited)"
        return "every type"

    def _summarise_excludes(self) -> str:
        return _excludes_summary(self._fields)

    def _set_follow(self, value: bool) -> None:
        self._fields["follow_symlinks"] = bool(value)

    def _filter_with_status(self) -> str:
        """Trailing column for the Frontmatter filter row — shows the
        DSL string plus a live ``✓`` / ``✗ col N`` parse indicator so
        syntax mistakes surface without leaving the form."""
        text = str(self._fields.get("filter") or "").strip()
        if not text:
            # Same reason as `_summarise_includes`: an unset rule here means
            # the default's rule applies, not that nothing does.
            inherited = _default_frontmatter(self.app).strip()
            return f"{inherited} (inherited)" if inherited else "(none)"
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
            _discard_custom_globs(self, "excludes_custom")
        self.query_one(SettingsList).refresh_values()

    def _prompt_custom(self, field_key: str, label: str) -> None:
        """Open the wizard's EditBar to capture a custom glob value and
        store it in ``self._fields[field_key]`` on submit."""
        item = MenuItem(
            id=f"wiz.{field_key}",
            label=label,
            hint=_GLOB_HINT,
            kind=KIND_SCALAR,
            value_getter=lambda _app, key=field_key: str(self._fields.get(key) or ""),
        )
        self.query_one(EditBar).open(item, _custom_seed(self, field_key))

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

    def unsaved_work(self) -> tuple[str, Callable[[], None]] | None:
        if self._fields == getattr(self, "_opened_with", self._fields):
            return None
        return "this collection", self.action_save_close

    def save_blocked(self) -> str:
        """As on the source form: the one answer both the save and the leaving
        prompt read."""
        from pathlib import Path

        from fnd.config import InvalidCollectionNameError, validate_collection_name

        name = str(self._fields["name"]).strip()
        if not name:
            return "Name is required."
        try:
            validate_collection_name(name)
        except InvalidCollectionNameError as e:
            return str(e)
        cfg = self.app._config  # type: ignore[attr-defined]
        if cfg is not None and name in cfg.collections:
            return f"Collection {name!r} already exists."
        path = str(self._fields["path"]).strip().strip("'\"")
        if not path:
            return "Source path is required."
        expanded = Path(path).expanduser()
        if path_is_absent(expanded):
            return f"Path does not exist: {expanded}"
        return ""

    def action_back(self) -> None:
        _leave_or_confirm(
            self,
            dirty=self._fields != getattr(self, "_opened_with", self._fields),
            what="this collection",
            on_save=self.action_save_close,
        )

    def action_save_close(self) -> None:
        # An open edit bar holds a value the user has typed but not submitted;
        # saving over the top of it dropped that value silently.
        if _commit_then(self, self.action_save_close):
            return
        from pathlib import Path

        from fnd.config import (
            EXCLUDES_PRESETS,
            CollectionConfig,
            InvalidCollectionNameError,
            SourceConfig,
            default_config_path,
            load,
            write_collection,
        )

        self._clear_error()

        name = str(self._fields["name"]).strip()
        path = str(self._fields["path"]).strip().strip("'\"")
        # Validated up-front so the user sees a focused error instead of a
        # crash from deep inside write_collection if they typed something
        # the persistence layer would reject (path separators, quotes,
        # control chars, …). Spaces ARE allowed — see validate_collection_name.
        if blocked := self.save_blocked():
            self._show_error(blocked)
            return
        p = Path(path).expanduser()

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

        # The row shows a live ✗ col N but nothing stopped a save, and the
        # model validates the rule, so an invalid one reached the user as an
        # unhandled ValidationError with the whole form's input lost.
        try:
            source = SourceConfig(
                path=p,
                includes=includes_globs,
                excludes=excludes_globs,
                follow_symlinks=bool(self._fields["follow_symlinks"]),
                frontmatter_filter=None,
                filters=_source_filters_or_none(
                    _merge_frontmatter(
                        dict(self._fields.get("filters", {})),
                        str(self._fields["filter"]),
                        _default_frontmatter(self.app),
                        had_override=self._fields.get("filters", {}).get("frontmatter") is not None,
                    )
                ),
            )
        except ValueError as e:
            # pydantic's ValidationError is a ValueError; `_summarise` renders
            # it as the field and message the row already showed.
            self._show_error(_summarise(e))
            return
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
        widgets = _focus_targets(self)
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

    CSS = chrome_css("NewCollectionScreen")

    def compose(self) -> ComposeResult:
        with Vertical(id="settings_box") as box:
            box.border_title = "Collections › New collection"
            yield Input(placeholder="Collection name (e.g. research)", id="new_collection_name")
        yield Static("", id="footer_hints")

    def on_mount(self) -> None:
        self.query_one("#new_collection_name", Input).focus()
        self._render_footer()

    def _render_footer(self) -> None:
        self.query_one("#footer_hints", Static).update(
            _editor_hint_bar((("⏎", "Create"), ("Esc", "Cancel")))
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

    CSS = chrome_css("RenameCollectionScreen")

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
        self.query_one("#footer_hints", Static).update(
            _editor_hint_bar((("⏎", "Save"), ("Esc", "Cancel")))
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
        # The file, not `app._config`: the collection is copied whole under the
        # new name, so a stale model drops or resurrects sources.
        try:
            cfg = load()
        except Exception as e:
            self.notify(
                f"The config file cannot be read, so nothing was renamed: {_summarise(e)}",
                severity="error",
                timeout=8,
            )
            return
        if self._old_name not in cfg.collections:
            app._config = cfg  # type: ignore[attr-defined]
            self.notify(
                f"{self._old_name!r} is no longer in the config file, so nothing was renamed.",
                severity="error",
                timeout=8,
            )
            self.app.pop_screen()
            return
        if new_name in cfg.collections:
            self.notify(f"{new_name!r} already exists", severity="warning")
            return
        busy = _indexing_now(app)
        if busy is not None:
            self.notify(
                f"Indexing {busy!r} is still running. Renaming now would leave "
                f"{self._old_name!r}'s documents in the index with nothing able to "
                "reach them. Cancel it or let it finish first.",
                severity="warning",
                timeout=8,
            )
            return
        existing = cfg.collections[self._old_name]
        write_collection(
            config_path=default_config_path(),
            name=new_name,
            collection=existing,
        )
        delete_collection(
            config_path=default_config_path(), name=self._old_name, renamed_to=new_name
        )
        app._config = load()  # type: ignore[attr-defined]
        app._scope.refresh_collections_panel()  # type: ignore[attr-defined]
        # Pop twice — past Rename and the now-stale per-collection
        # screen — before pushing the IndexerScreen.
        self.app.pop_screen()
        self.app.pop_screen()
        # Confirmed like the other acts that empty the index: this drops the
        # old name's documents and rebuilds from scratch, which on a large
        # collection is minutes, from a single Enter in a text field.
        self.app.push_screen(
            RebuildConfirmScreen(
                collection_name=new_name,
                crumb="Rename",
                body=(
                    f"Renamed to {new_name!r}. Reindex it now?\n\n"
                    f"The old name's documents are dropped and {new_name!r} is "
                    "built from scratch, which takes as long as indexing it "
                    "does. Until it finishes, this collection holds less than "
                    "it does now.\n\n"
                    "The config is already saved either way, and the files on "
                    "disk are untouched. Skipping leaves the old name's "
                    "documents in the index: nothing can reach them once the "
                    "config no longer names that collection, and no later run "
                    "removes them. Reindexing now is what clears them."
                ),
                confirm_label=f"Yes, reindex {new_name}",
                # NOT "Cancel": the rename is already written, and only the
                # reindex is on offer here. A user reading "Cancel" reasonably
                # expects the rename undone, and it is not.
                decline_label="No, leave the index for now",
                on_confirm=lambda: self._drop_old_then_reindex(app, new_name),
            )
        )

    def _drop_old_then_reindex(self, app: FNDApp, new_name: str) -> None:
        """The old name's documents go before the new name's are built.

        Nothing can reach them once the config no longer names them: Delete is
        the only caller that drops by collection, and a rebuild only touches
        the names the config still has. Sequential because tantivy takes one
        writer, and threaded because the drop is seconds on a fragmented index.
        """
        import contextlib

        from fnd.index import drop_collection

        old = self._old_name
        index_dir = app._index_dir  # type: ignore[attr-defined]

        def _then(error: str | None) -> None:
            if error:
                app.notify(
                    f"{old!r} could not be dropped from the index: {error}", severity="error"
                )
            app._indexer.reindex_with_warning(new_name, rebuild=True)  # type: ignore[attr-defined]

        _defaults = getattr(getattr(app, "_config", None), "defaults", None)

        def _work() -> None:
            error: str | None = None
            try:
                drop_collection(
                    index_dir,
                    old,
                    tag_sources=tuple(_defaults.tag_sources)
                    if _defaults
                    else ("frontmatter", "os"),
                    tag_frontmatter_keys=tuple(_defaults.tag_frontmatter_keys) if _defaults else (),
                )
            except Exception as e:
                error = str(e)
            with contextlib.suppress(Exception):
                app.call_from_thread(_then, error)

        app.run_worker(_work, thread=True, exclusive=True, group=f"rename-{old}")

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

    CSS = (
        chrome_css("DeleteCollectionScreen", confirm=True)
        + """
    DeleteCollectionScreen #confirm_summary { padding: 0 0 1 0; }
    DeleteCollectionScreen #deleting_status { padding: 1 0; color: $text-muted; }
    DeleteCollectionScreen #deleting_spinner { height: 1; }
    DeleteCollectionScreen .-hidden { display: none; }
    """
    )

    def __init__(self, *, collection_name: str) -> None:
        super().__init__()
        self._name = collection_name
        # Set once the worker is dispatched: freezes the bindings so the user
        # can't re-fire "Yes" (a second worker) or escape onto the now-stale
        # parent screen mid-delete. Never cleared — the screen is single-use.
        self._deleting = False
        self._default_moved = False

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
            yield ConfirmList(
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
        enter = open_confirm_list(self, land_on="no")
        app: FNDApp = self.app  # type: ignore[assignment]
        self.query_one("#footer_hints", Static).update(
            _hint_bar(app, (("↑↓", "Choose"), enter, ("Esc", "Cancel")))
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
        busy = _indexing_now(app)
        if busy is not None:
            self.notify(
                f"Indexing {busy!r} is still running. Deleting now would leave "
                f"{self._name!r}'s documents in the index with nothing able to "
                "reach them. Cancel it or let it finish first.",
                severity="warning",
                timeout=8,
            )
            return
        self._default_moved = delete_collection(config_path=default_config_path(), name=self._name)
        app._config = load()  # type: ignore[attr-defined]
        self._show_deleting()
        name = self._name
        index_dir = app._index_dir  # type: ignore[attr-defined]
        _defaults = getattr(getattr(app, "_config", None), "defaults", None)

        def _drop() -> str | None:
            from fnd.index import drop_collection

            try:
                drop_collection(
                    index_dir,
                    name,
                    tag_sources=tuple(_defaults.tag_sources)
                    if _defaults
                    else ("frontmatter", "os"),
                    tag_frontmatter_keys=tuple(_defaults.tag_frontmatter_keys) if _defaults else (),
                )
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
        else:
            # Two screens pop and the row is gone; nothing said the act had
            # happened, which on an irreversible one is the moment to say it.
            app.notify(f"{self._name!r} deleted. The files on disk are untouched.")
        if self._default_moved:
            app.notify(
                f"{self._name!r} was your default collection. Searches now cover every one.",
                severity="warning",
            )
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

    CSS = (
        chrome_css("CacheMaintenanceConfirm", confirm=True)
        + """
    CacheMaintenanceConfirm > #settings_box { border: round $warning; }
    CacheMaintenanceConfirm.-destructive > #settings_box { border: round $error; }
    CacheMaintenanceConfirm #confirm_summary { padding: 0 0 1 0; }
    CacheMaintenanceConfirm #confirm_irreversible {
        color: $error; text-style: bold; padding: 0 0 1 0;
    }
    """
    )

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
            yield ConfirmList(
                Option(Text(self._confirm_label, style="bold"), id="yes"),
                Option("Cancel", id="no"),
                id="confirm_list",
            )
        yield Static("", id="footer_hints")

    def on_mount(self) -> None:
        enter = open_confirm_list(self, land_on="no" if self._irreversible else "")
        app: FNDApp = self.app  # type: ignore[assignment]
        self.query_one("#footer_hints", Static).update(
            _hint_bar(app, (("↑↓", "Nav"), enter, ("Esc", "Cancel")))
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
            return "Rebuild: drop chunks and re-texturise every PDF from scratch"
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
                    "Costly: use to rebuild all previews under the current engine.\n"
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
            n = len(self._names)
            confirm = "Yes, update it" if n == 1 else f"Yes, update all {n} collections"
            yield ConfirmList(
                Option(Text(confirm, style="bold green"), id="yes"),
                Option("Cancel", id="no"),
                id="confirm_list",
            )
        yield Static("", id="footer_hints")

    def on_mount(self) -> None:
        enter = open_confirm_list(self)
        app: FNDApp = self.app  # type: ignore[assignment]
        self.query_one("#footer_hints", Static).update(
            _hint_bar(app, (("↑↓", "Nav"), enter, ("Esc", "Cancel")))
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
            yield ConfirmList(
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
        enter = open_confirm_list(self)
        app: FNDApp = self.app  # type: ignore[assignment]
        self.query_one("#footer_hints", Static).update(
            _hint_bar(app, (("↑↓", "Nav"), enter, ("Esc", "Cancel")))
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


# ── Clone-source flow ───────────────────────────────────────────────


def _indexing_now(app: FNDApp) -> str | None:
    """The collection being indexed, if a run holds the index writer.

    Renaming or deleting pairs a config write with a drop from the index, and
    the drop needs that writer. Mid-run it cannot have it, so the drop fails
    after the config write has landed, and the running task goes on writing
    under a name nothing can reach afterwards.
    """
    service = getattr(app, "_indexer", None)
    task = getattr(service, "task", None)
    if task is None or task.done():
        return None
    return str(getattr(service, "collection", "") or "another collection")


class UnsavedChangesScreen(Screen[None]):
    """Save, discard, or stay: for a screen holding work that is not on disk.

    Esc on an editing screen asks rather than throwing the work away, so a
    user who cannot lose work does not have to know which key saves.
    """

    BINDINGS = [  # noqa: RUF012
        Binding("escape,left", "back", "Keep editing", show=False),
        Binding("up,k", "cursor(-1)", show=False),
        Binding("down,j", "cursor(1)", show=False),
        Binding("enter", "activate", show=False),
        # The question is "are you sure you want to quit"; `q` reaching the
        # app's quit through it would answer yes by pressing it again.
        Binding("q", "back", "Keep editing", show=False, priority=True),
    ]

    CSS = chrome_css("UnsavedChangesScreen", confirm=True)

    def __init__(
        self,
        *,
        what: str,
        on_save: Callable[[], None] | None,
        on_leave: Callable[[], None] | None = None,
        leave_label: str = "Discard changes",
        blocked: str = "",
    ) -> None:
        super().__init__()
        self._what = what
        # A save the screen below has already refused loops: it repaints
        # nothing, so pressing the default again looks like a dead key.
        self._blocked = blocked
        self._on_save = None if blocked else on_save
        self._leave_action = on_leave
        self._leave_label = leave_label

    def compose(self) -> ComposeResult:
        with Vertical(id="settings_box") as box:
            box.border_title = "Unsaved changes"
            # Subject-agnostic: the subjects are a mix of singular and plural
            # ("this source", "these filters"), and a sentence carrying its own
            # verb would read "These filters has changes that are not saved."
            yield Static(f"Unsaved changes to {self._what}.", classes="warning")
            if self._blocked:
                yield Static(f"Cannot save yet: {self._blocked}", classes="warning")
            # Save is offered only where the work is on the screen below this
            # one. A form buried under another editor cannot be saved from
            # here: its own save pops whatever is on top, which is not it.
            options = (
                [Option(Text("Save changes", style="bold"), id="save")] if self._on_save else []
            )
            options += [Option(self._leave_label, id="discard"), Option("Keep editing", id="stay")]
            yield ConfirmList(*options, id="confirm_list")
        yield Static("", id="footer_hints")

    def on_mount(self) -> None:
        # Always the row that changes nothing. This dialog is reached by Esc,
        # which the editor's footer offers as a way OUT, so landing on "Save
        # changes" put a write one Enter from a key that means the opposite.
        open_confirm_list(self, land_on="stay")
        app: FNDApp = self.app  # type: ignore[assignment]
        self.query_one("#footer_hints", Static).update(
            _hint_bar(app, (("↑↓", "Choose"), ("⏎", "Select"), ("Esc", "Keep editing")))
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
    def _chosen(self, ev: OptionList.OptionSelected) -> None:
        choice = ev.option.id
        self.app.pop_screen()
        if choice == "save" and self._on_save is not None:
            # The editor's own save pops it, and reports its own failure.
            self._on_save()
        elif choice == "discard":
            with contextlib.suppress(Exception):
                if self._leave_action is not None:
                    self._leave_action()
                else:
                    self.app.pop_screen()


def _leave_or_confirm(
    screen: Screen[None], *, dirty: bool, what: str, on_save: Callable[[], None]
) -> None:
    """Leave, or ask first. Unsaved work never leaves without being offered."""
    if not dirty:
        screen.app.pop_screen()
        return
    screen.app.push_screen(
        UnsavedChangesScreen(what=what, on_save=on_save, blocked=save_blocked_on(screen))
    )


def save_blocked_on(screen: object) -> str:
    """Why the screen cannot save what it is holding, or "".

    A screen answers by exposing ``save_blocked``; anything else can save.
    """
    ask = getattr(screen, "save_blocked", None)
    if not callable(ask):
        return ""
    try:
        return str(ask() or "")
    except Exception:
        return ""


def unsaved_on_stack(
    screens: Sequence[object],
) -> tuple[str, Callable[[], None] | None, str] | None:
    """What the SCREEN STACK would lose, topmost holder first, and why saving
    it here is not on offer.

    Every screen is asked, not only the top one: the filter browser is only
    ever pushed on top of the source form, so a dirty form can sit under a
    clean browser. The saver comes back only for the topmost screen: a form
    buried under another editor cannot be saved from a modal, because its own
    save pops whatever is on top of it.
    """
    for depth, screen in enumerate(reversed(list(screens))):
        answer = unsaved_on(screen)
        if answer is not None:
            what, save = answer
            if depth:
                return what, None, "the screen holding it is behind this one"
            return what, save, save_blocked_on(screen)
    return None


def unsaved_on(screen: object) -> tuple[str, Callable[[], None]] | None:
    """What a screen would lose if it were left now, and how to save it.

    One seam so `q` can ask the same question Esc does. A screen answers by
    exposing ``unsaved_work``; anything else has nothing to lose.
    """
    ask = getattr(screen, "unsaved_work", None)
    if not callable(ask):
        return None
    try:
        answer = ask()
    except Exception:
        return None
    if not isinstance(answer, tuple) or len(answer) != 2:
        return None
    what, save = answer
    return str(what), cast("Callable[[], None]", save)


class RebuildConfirmScreen(Screen[None]):
    """Confirm an act that empties the index before refilling it.

    Delete-source, delete-collection and Update-all confirm, and so must the
    acts that empty an index. Rebuild sits one row under "Update index" on
    the same panel, and a rename drops the old name's documents and rebuilds
    from the field you typed in: both one Enter away, both differing from
    their harmless neighbour only in cost and consequence, which is exactly
    what a label cannot carry alone.

    One screen, two callers: the wording differs because the acts do, but a
    second class would be a second dialog to keep in step.
    """

    BINDINGS = [  # noqa: RUF012
        Binding("escape,left", "back", "Cancel", show=False),
        Binding("up,k", "cursor(-1)", show=False),
        Binding("down,j", "cursor(1)", show=False),
        Binding("enter", "activate", show=False),
    ]

    CSS = chrome_css("RebuildConfirmScreen", confirm=True)

    def __init__(
        self,
        *,
        collection_name: str,
        on_confirm: Callable[[], None],
        crumb: str = "Rebuild",
        body: str = "",
        confirm_label: str = "",
        decline_label: str = "Cancel",
    ) -> None:
        super().__init__()
        self._collection_name = collection_name
        self._on_confirm = on_confirm
        self._crumb = crumb
        self._body = body
        self._confirm_label = confirm_label
        self._decline_label = decline_label

    def compose(self) -> ComposeResult:
        name = self._collection_name
        body = self._body or (
            f"Rebuild {name!r} from scratch?\n\n"
            "Its chunks are dropped first, so until the run finishes this "
            "collection holds less than it does now, and a rebuild that "
            "is cancelled or interrupted leaves it part-built.\n\n"
            "The files on disk are untouched. Update index adds and drops "
            "what changed without emptying anything, and is what you want "
            "unless you are re-texturising after an engine upgrade."
        )
        with Vertical(id="settings_box") as box:
            box.border_title = f"Collections › {name} › {self._crumb}"
            yield Static(body, classes="warning")
            yield ConfirmList(
                Option(Text(self._confirm_label or f"Yes, rebuild {name}", style="bold"), id="yes"),
                Option(self._decline_label, id="no"),
                id="confirm_list",
            )
        yield Static("", id="footer_hints")

    def on_mount(self) -> None:
        enter = open_confirm_list(self, land_on="no")
        app: FNDApp = self.app  # type: ignore[assignment]
        self.query_one("#footer_hints", Static).update(
            _hint_bar(app, (("↑↓", "Choose"), enter, ("Esc", self._decline_label)))
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
    def _chosen(self, ev: OptionList.OptionSelected) -> None:
        confirmed = ev.option.id == "yes"
        self.app.pop_screen()
        if confirmed:
            self._on_confirm()


def _find_source(sources: Sequence[Any], path: str, hint: int) -> int | None:
    """Row of the source at ``path``: ``hint`` if it still holds it, else its only row."""
    if 0 <= hint < len(sources) and str(sources[hint].path) == path:
        return hint
    rows = [i for i, src in enumerate(sources) if str(src.path) == path]
    return rows[0] if len(rows) == 1 else None


class DeleteSourceScreen(Screen[None]):
    """Confirm + remove a single source from a collection.

    Triggered by ``Ctrl+D`` inside :class:`SourceFormScreen` (only when
    editing an existing source). The source's path is dropped from
    ``[collections.<name>.sources]`` via :func:`fnd.config.write_collection`.
    Reindex of the collection follows because the source set changed.

    The source is found by its path in the file as it is at each step, never by
    row index into ``app._config``: any reload replaces that model, and
    ``write_collection`` writes the whole collection table back.
    """

    BINDINGS = [  # noqa: RUF012
        Binding("escape,left", "back", "Cancel", show=False),
        Binding("up,k", "cursor(-1)", show=False),
        Binding("down,j", "cursor(1)", show=False),
        Binding("enter", "activate", show=False),
    ]

    CSS = chrome_css("DeleteSourceScreen", confirm=True)

    def __init__(
        self, *, collection_name: str, source_index: int, source_path: str | None = None
    ) -> None:
        super().__init__()
        self._collection_name = collection_name
        self._source_index = source_index
        # None pins whichever source sits at `source_index` when the dialog opens.
        self._source_path = source_path

    def _locate(self) -> tuple[Any, int | None, str]:
        """The config as the file holds it now, this source's row in it, and why not.

        A read that cannot find the source is adopted as ``app._config``, so
        reopening Sources shows the file rather than the model that lost it.
        """
        from fnd.config import load

        try:
            cfg = load()
        except Exception as e:
            return None, None, f"The config file cannot be read: {_summarise(e)}"
        name = self._collection_name
        col = cfg.collections.get(name)
        sources = [] if col is None else col.sources
        if self._source_path is None and 0 <= self._source_index < len(sources):
            self._source_path = str(sources[self._source_index].path)
        index = _find_source(sources, self._source_path or "", self._source_index)
        if index is not None:
            return cfg, index, ""
        self.app._config = cfg  # type: ignore[attr-defined]
        where = f"\nPath: {self._source_path}" if self._source_path else ""
        return cfg, None, f"This source is no longer in {name!r} in the config file.{where}"

    def compose(self) -> ComposeResult:
        cfg, index, problem = self._locate()
        name = self._collection_name
        with Vertical(id="settings_box") as box:
            box.border_title = (
                f"Collections › {name} › Sources › "
                f"Source {(self._source_index if index is None else index) + 1} › Delete"
            )
            if problem:
                yield Static(
                    f"{problem}\n\nNothing was removed. Press Esc and reopen Sources "
                    "to see the file as it is now.",
                    classes="warning",
                )
                yield ConfirmList(Option("Back", id="no"), id="confirm_list")
            else:
                # "Files another source still reaches stay" is false comfort
                # where there is no other source: everything this one reached leaves.
                shared = (
                    "Files another source still reaches stay."
                    if len(cfg.collections[name].sources) > 1
                    else "It is the only source, so the collection is left empty."
                )
                yield Static(
                    f"Remove this source from {name!r}?\n"
                    f"Path: {self._source_path}\n\n"
                    "The files on disk are untouched.\n"
                    f"{name!r} is rebuilt straight afterwards, which "
                    "takes as long as indexing it does and drops the chunks only "
                    f"this source reached. {shared}",
                    classes="warning",
                )
                yield ConfirmList(
                    Option(Text("Yes, remove this source", style="bold"), id="yes"),
                    Option("Cancel", id="no"),
                    id="confirm_list",
                )
        yield Static("", id="footer_hints")

    def on_mount(self) -> None:
        enter = open_confirm_list(self, land_on="no")
        app: FNDApp = self.app  # type: ignore[assignment]
        self.query_one("#footer_hints", Static).update(
            _hint_bar(app, (("↑↓", "Choose"), enter, ("Esc", "Cancel")))
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
        cfg, index, problem = self._locate()
        if index is None:
            self.notify(f"{problem}\nNothing was removed.", severity="error", timeout=8)
            self.app.pop_screen()
            return
        col = cfg.collections[self._collection_name]
        busy = _indexing_now(app)
        if busy is not None:
            # The dialog promises the collection is rebuilt straight
            # afterwards. Mid-run that rebuild is refused and dropped, so the
            # removed source's files stay searchable and the promise is false.
            self.notify(
                f"Indexing {busy!r} is still running, and removing a source "
                "rebuilds the collection. Cancel it or let it finish first.",
                severity="warning",
                timeout=8,
            )
            return
        del col.sources[index]
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

    CSS = chrome_css("CloneSourcePickCollectionScreen")

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

    CSS = chrome_css("CloneSourcePickSourceScreen")

    def __init__(self, *, source_collection: str, target_collection: str) -> None:
        super().__init__()
        self._source_coll = source_collection
        self._target = target_collection
        self._paths: list[str] = []

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
                self._paths = [str(src.path) for src in sources]
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
                    label = f"{i + 1}. {base}  ·  {types}  ·  {_display_path(str(src.path))}"
                    # Wrapped, a long path reads as another source in the list.
                    options.append(
                        Option(Text(label, no_wrap=True, overflow="ellipsis"), id=str(i))
                    )
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

        app: FNDApp = self.app  # type: ignore[assignment]
        # `clone_source` indexes the file, and these rows came from `app._config`.
        try:
            cfg = load()
        except Exception as e:
            self.notify(
                f"The config file cannot be read, so nothing was cloned: {_summarise(e)}",
                severity="error",
                timeout=8,
            )
            return
        col = cfg.collections.get(self._source_coll)
        found = _find_source([] if col is None else col.sources, self._paths[idx], idx)
        if found is None:
            app._config = cfg  # type: ignore[attr-defined]
            self.notify(
                f"That source is no longer in {self._source_coll!r} in the config file, "
                "so nothing was cloned.",
                severity="error",
                timeout=8,
            )
            return
        try:
            clone_source(
                config_path=default_config_path(),
                source_collection=self._source_coll,
                source_index=found,
                target_collection=self._target,
            )
        except (KeyError, IndexError, ValueError) as e:
            self.notify(f"Clone failed: {e}", severity="error")
            return

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


SEARCH_PLACEHOLDER = "Filter rows…"
_SEARCH_PLACEHOLDER_WITH_KEY = f"{SEARCH_PLACEHOLDER}  (/)"


def refresh_search_placeholder(screen: Screen[None], selector: str) -> None:
    """Offer `(/)` only where `/` reaches the binding.

    The box opens WITH focus, so the advertised key goes in as text and the
    empty state then blames the terms: `No matches for '/collect'`.
    """
    import contextlib

    with contextlib.suppress(Exception):
        box = screen.query_one(selector, Input)
        box.placeholder = SEARCH_PLACEHOLDER if box.has_focus else _SEARCH_PLACEHOLDER_WITH_KEY


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
        title = "Flat PDFs: review & retry"
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


class FilterTextScreen(Screen[None]):
    """Edit a filter set as one expression.

    The rows and this text are two views of the same set: a clause typed here
    that matches a row's shape becomes that row when saved.
    """

    BINDINGS = [  # noqa: RUF012
        Binding("escape", "back", "Back", show=False),
        Binding("ctrl+s", "save_close", show=False),
    ]

    CSS = """
    FilterTextScreen { background: $surface; }
    FilterTextScreen > #settings_box {
        height: 1fr; border: round $primary 50%; padding: 0 1;
    }
    FilterTextScreen > #settings_box:focus-within { border: round $accent; }
    FilterTextScreen #filter_text { height: 1fr; }
    FilterTextScreen #filter_status { height: auto; padding: 0 1; color: $text-muted; }
    FilterTextScreen #filter_status.-ok { color: $success; }
    FilterTextScreen #filter_status.-bad { color: $error; }
    FilterTextScreen > #footer_hints {
        dock: bottom; height: 1; background: $surface; padding: 0 1; color: $text-muted;
    }
    """

    def __init__(
        self,
        *,
        title: str,
        spec: Any,
        on_save: Callable[[Any], None],
    ) -> None:
        super().__init__()
        self._title = title
        self._spec = spec
        self._on_save = on_save

    def compose(self) -> ComposeResult:
        from fnd.filters.text_form import render

        with Vertical(id="settings_box") as box:
            box.border_title = self._title
            yield TextArea(render(self._spec), id="filter_text")
            yield Static("", id="filter_status")
        yield Static("", id="footer_hints")

    def on_mount(self) -> None:
        self.query_one("#filter_text", TextArea).focus()
        self._refresh_status()
        self.query_one("#footer_hints", Static).update(
            # Applies, never saves: both editors hand back to the filter
            # browser, which decides whether anything reaches disk.
            _editor_hint_bar(((COMMIT_KEY, "Apply"), ("Esc", "Cancel")))
        )

    @on(TextArea.Changed, "#filter_text")
    def _on_changed(self, _ev: TextArea.Changed) -> None:
        self._refresh_status()

    def _parsed(self) -> tuple[Any, Any]:
        from fnd.filters.text_form import parse_or_error

        return parse_or_error(self.query_one("#filter_text", TextArea).text)

    def _refresh_status(self) -> None:
        status = self.query_one("#filter_status", Static)
        spec, err = self._parsed()
        status.remove_class("-ok", "-bad")
        if err is not None:
            status.add_class("-bad")
            status.update(f"✗ col {err.column}: {err.message}")
            return
        status.add_class("-ok")
        rows = _describe_spec(spec)
        dropped = _protection_dropped(self._spec, spec)
        if dropped:
            # Replacing the text is how the guard leaves: it is rendered into
            # the box, so deleting it reads as typing one rule.
            status.add_class("-bad")
            status.remove_class("-ok")
            status.update(
                f"⚠ this drops the {dropped} exclusion: {rows}"
                if rows
                else f"⚠ this drops the {dropped} exclusion"
            )
            return
        status.update(f"✓ {rows}" if rows else "✓ no filters")

    def action_back(self) -> None:
        from fnd.filters.text_form import render

        typed = self.query_one("#filter_text", TextArea).text.strip()
        _leave_or_confirm(
            self,
            dirty=typed != render(self._spec).strip(),
            what="this filter text",
            on_save=self.action_save_close,
        )

    def save_blocked(self) -> str:
        """Why Apply would be refused, or "".

        The leaving prompt reads this, as the source form's does, so it never
        offers to save text the screen has already rejected.
        """
        _spec, err = self._parsed()
        return f"col {err.column}: {err.message}" if err is not None else ""

    def action_save_close(self) -> None:
        spec, err = self._parsed()
        if err is not None or spec is None:
            # The status line may already be showing this error, in which case
            # refreshing it changes nothing on screen and the key reads dead:
            # measured at 14 identical pane captures over 3.5 seconds.
            self._refresh_status()
            self.app.notify(f"Not applied: {self.save_blocked()}", severity="error", timeout=4)
            return
        self._on_save(spec)
        self.app.pop_screen()


def _protection_dropped(before: Any, after: Any) -> str:
    """A guard tag the edit would remove, or "".

    ``no_index`` is the one exclusion a user cannot see the effect of until a
    file they meant to keep private turns up in results.
    """
    from fnd.tui.widgets.toggle_tree import NEVER_ONLY_TAGS

    def _tags(spec: Any) -> set[str]:
        return {t for tags in spec.exclude_tags.values() for t in tags}

    lost = sorted((_tags(before) - _tags(after)) & set(NEVER_ONLY_TAGS))
    return "/".join(lost)


def _any_tag(sample: Any) -> bool:
    """Whether a sample offers a single tag.

    ``sample_source`` seeds ``tags`` with an empty dict per provider, so the
    mapping is truthy on a source that carries none.
    """
    tags = getattr(sample, "tags", None) or {}
    return any(values for values in tags.values())


def _describe_spec(spec: Any) -> str:
    """Which rows the text currently fills, so the effect is visible on save."""
    parts: list[str] = []
    if spec.kinds:
        parts.append(f"{len(spec.kinds)} kind" + ("s" if len(spec.kinds) > 1 else ""))
    for field_name, phrase in (("include_tags", "only files tagged"), ("exclude_tags", "never")):
        values = sorted({t for tags in getattr(spec, field_name).values() for t in tags})
        if values:
            parts.append(
                f"{phrase} {'/'.join(values)}"
                if len(values) < 4
                else f"{phrase} {len(values)} tags"
            )
    if spec.min_size is not None or spec.max_size is not None:
        parts.append("size")
    if any(
        getattr(spec, f) is not None
        for f in ("created_after", "created_before", "modified_after", "modified_before")
    ):
        parts.append("dates")
    if spec.frontmatter:
        parts.append("frontmatter")
    if spec.expression or spec.raw:
        parts.append("custom")
    return " · ".join(parts)


_SPEC_FIELDS = (
    "kinds",
    "include_tags",
    "exclude_tags",
    "min_size",
    "max_size",
    "created_after",
    "created_before",
    "modified_after",
    "modified_before",
    "frontmatter",
    "expression",
)


def _spec_from_filters(filters: Any) -> Any:
    """A ``DefaultFilters``/``SourceFilters`` as the text form's spec.

    The two ignore-file toggles have no expression form (they select which
    files are read, not a predicate over one), so they stay on their rows.
    """
    from fnd.filters import FilterSpec
    from fnd.filters.dimensions import tag_selection

    values: dict[str, Any] = {}
    for name in _SPEC_FIELDS:
        value = getattr(filters, name, None)
        if value is None:
            continue
        if name in ("include_tags", "exclude_tags"):
            values[name] = tag_selection(value)
        else:
            values[name] = tuple(value) if isinstance(value, list) else value
    return FilterSpec(**values)


def _spec_to_mapping(spec: Any) -> dict[str, Any]:
    """The spec's fields as config values, with the text form's leftovers
    folded back into ``expression`` so nothing typed is lost."""
    out: dict[str, Any] = {}
    for name in _SPEC_FIELDS:
        value = getattr(spec, name)
        if isinstance(value, dict):
            # Every source holding the same tags is the bare list the user
            # most likely typed; anything else needs the table to stay exact.
            from fnd.tags import TAG_PROVIDERS

            sets = {source: frozenset(tags) for source, tags in value.items() if tags}
            uniform = len(sets) == len(TAG_PROVIDERS) and len(set(sets.values())) == 1
            # Empty stays a list: it is the explicit "override the default to
            # nothing", and a bare list is how that reads in the config.
            out[name] = (
                (
                    sorted(next(iter(sets.values())))
                    if uniform or not sets
                    else {source: sorted(tags) for source, tags in sets.items()}
                )
                if sets
                else []
            )
        else:
            out[name] = list(value) if isinstance(value, tuple) else value
    if spec.raw:
        joined = " AND ".join(f"({c})" for c in (spec.expression, *spec.raw) if c)
        out["expression"] = joined
    return out


def _matching_group(group: ToggleGroup, query: str) -> ToggleGroup | None:
    """The group with only the rows that match, or None when none do.

    A group whose own name matches keeps everything under it, so searching for
    a branch shows the branch rather than emptying it.
    """
    from dataclasses import replace

    if query in group.label.lower():
        return group
    items = tuple(i for i in group.items if query in i.label.lower())
    groups = tuple(g for g in (_matching_group(s, query) for s in group.groups) if g is not None)
    if not items and not groups:
        return None
    # Kept so the branch's roll-up still speaks for the whole branch: counted
    # over the surviving rows alone it read `● File types (every type)` with
    # one of forty ticked. Each level holds only what it dropped itself.
    kept_items = {i.id for i in items}
    kept_groups = {g.id for g in groups}
    hidden = tuple(i for i in group.items if i.id not in kept_items) + tuple(
        leaf for sub in group.groups if sub.id not in kept_groups for leaf in sub.leaves
    )
    return replace(group, items=items, groups=groups, hidden=hidden)


def _branch_group(branch: Any) -> ToggleGroup:
    """A model :class:`Branch` as the widget's :class:`ToggleGroup`, nested."""
    return ToggleGroup(
        id=branch.id,
        label=branch.label,
        items=tuple(ToggleItem(*i) for i in branch.items),
        mode=branch.mode,
        empty_label=branch.empty_label,
        full_label=branch.full_label,
        noun=branch.noun,
        name_leaves=branch.name_leaves,
        elsewhere=branch.elsewhere,
        complete=branch.complete,
        groups=tuple(_branch_group(b) for b in branch.groups),
    )


#: Shown under the rule box, where an inert glob is easy to write: `*` does
#: not cross `/`, so `'*drafts*'` never matches while `'drafts/**'` does, and
#: a bare folder name matches only a FILE of that name.
_GLOB_HINT = "'build/**' for a folder; 'build' matches only a file called build"

_RULE_HELP = (
    "fields  file.path/name/ext/kind/size, file.tags.all, or any frontmatter key\n"
    "match   ~~ is a glob and * stops at /, so 'drafts/**' matches, '*drafts*' does not"
)


class RuleTextScreen(Screen[None]):
    """One typed filter rule, validated as you type.

    Separate from :class:`FilterTextScreen`, which edits the whole set: a row
    that opens the entire expression to change one clause is a trap.
    """

    BINDINGS = [  # noqa: RUF012
        Binding("escape", "back", "Back", show=False),
        Binding("ctrl+s", "save_close", show=False),
    ]

    CSS = """
    RuleTextScreen { background: $surface; }
    RuleTextScreen > #settings_box {
        height: 1fr; border: round $primary 50%; padding: 0 1;
    }
    RuleTextScreen > #settings_box:focus-within { border: round $accent; }
    RuleTextScreen #rule_text { height: 1fr; }
    RuleTextScreen #rule_status { height: auto; padding: 0 1; color: $text-muted; }
    RuleTextScreen #rule_status.-ok { color: $success; }
    RuleTextScreen #rule_status.-bad { color: $error; }
    RuleTextScreen #rule_help {
        height: auto; padding: 0 1; color: $text-muted; text-style: dim;
    }
    RuleTextScreen > #footer_hints {
        dock: bottom; height: 1; background: $surface; padding: 0 1; color: $text-muted;
    }
    """

    def __init__(
        self,
        *,
        title: str,
        value: str,
        note_scoped: bool,
        on_save: Callable[[str], None],
    ) -> None:
        super().__init__()
        self._title = title
        self._value = value
        self._note_scoped = note_scoped
        self._on_save = on_save

    def compose(self) -> ComposeResult:
        with Vertical(id="settings_box") as box:
            box.border_title = self._title
            yield TextArea(self._value, id="rule_text")
            yield Static("", id="rule_status")
            yield Static(_RULE_HELP, id="rule_help")
        yield Static("", id="footer_hints")

    def on_mount(self) -> None:
        self.query_one("#rule_text", TextArea).focus()
        self._refresh_status()
        self.query_one("#footer_hints", Static).update(
            # Applies, never saves: both editors hand back to the filter
            # browser, which decides whether anything reaches disk.
            _editor_hint_bar(((COMMIT_KEY, "Apply"), ("Esc", "Cancel")))
        )

    @on(TextArea.Changed, "#rule_text")
    def _on_changed(self, _ev: TextArea.Changed) -> None:
        self._refresh_status()

    def _parsed(self) -> Any:
        from fnd.filter_dsl import parse_or_error

        text = self.query_one("#rule_text", TextArea).text.strip()
        return (None, None) if not text else parse_or_error(text)

    def _refresh_status(self) -> None:
        status = self.query_one("#rule_status", Static)
        _pred, err = self._parsed()
        status.remove_class("-ok", "-bad")
        if err is not None:
            status.add_class("-bad")
            status.update(f"✗ col {err.column}: {err.message}")
            return
        status.add_class("-ok")
        scope = "files with a frontmatter block" if self._note_scoped else "every file"
        # "✓ every file" alone reads as "this matches every file". It is the
        # rule's scope, and a rule that parses can still match nothing or
        # exclude nothing.
        status.update(f"✓ reads as valid: it will be tested against {scope}")

    def action_back(self) -> None:
        self.app.pop_screen()

    def action_save_close(self) -> None:
        _pred, err = self._parsed()
        if err is not None:
            self._refresh_status()
            return
        self._on_save(self.query_one("#rule_text", TextArea).text.strip())
        self.app.pop_screen()


#: Spec fields as the screens name them, so a message reads like the UI. Two
#: fields sharing a name collapse to one entry.
_FIELD_WORDS: dict[str, str] = {
    "kinds": "file types",
    "include_tags": "required tags",
    "exclude_tags": "skipped tags",
    "min_size": "size limits",
    "max_size": "size limits",
    "created_after": "created dates",
    "created_before": "created dates",
    "modified_after": "modified dates",
    "modified_before": "modified dates",
    "frontmatter": "the frontmatter rule",
    "expression": "the custom rule",
}


def _cleared_note(before: Any, after: Any) -> str:
    """What returning to the defaults just took away, named as screens name it.

    A set with nothing to return to refuses the act, so no sentence here claims
    an empty set, which would be false anyway: ignore files and hidden-name
    pruning survive any clear.
    """
    dropped = list(
        dict.fromkeys(
            _FIELD_WORDS[name]
            for name in _SPEC_FIELDS
            if getattr(before, name) and not getattr(after, name)
        )
    )
    if not dropped:
        return "Nothing to return"
    lost = ", ".join(dropped)
    return f"Back to the inherited filters: this source no longer overrides {lost}"


#: What the sidebar's clear bar answers to. Read once, like the app's own
#: bindings, so the two panes cannot drift onto different keys.
_CLEAR_FILTERS_KEY: str = load_keymap().for_action("clear_filters") or "X"


class FilterBrowserScreen(Screen[None]):
    """Filters as the Filters pane shows them: collapsible branches, tri-state.

    The same set is editable as text (``t``); each view writes the model the
    other reads, so neither is the source of truth.
    """

    BINDINGS = [  # noqa: RUF012
        Binding("escape,left", "back", "Back", show=False),
        # As every other settings list binds it, and this is the longest one:
        # a vault's tags run to thousands of rows reachable by arrow key alone.
        Binding("slash", "focus_search", "Filter", show=False),
        # Down from the filter box reaches the rows, as it does on every other
        # settings screen. Without it the box was a one-way door: narrow the
        # tree, then have no key that leaves the Input for what you narrowed.
        Binding("down", "tree_from_input", show=False),
        Binding("ctrl+s", "save_close", show=False),
        Binding("t", "edit_text", show=False),
        # The sidebar's own clear gesture, not a second letter for the same
        # act: one pane cleared on `X` from anywhere, the other on `c`, and a
        # single unconfirmed letter beside `t` and `y` wiped the set.
        Binding(_CLEAR_FILTERS_KEY, "clear_all", show=False),
        # Not ctrl+y: the app binds that to "copy query command" with
        # priority, so a screen binding there never fires.
        Binding("y", "copy_text", show=False),
        # `?` does not come back here: it lands on the settings menu, taking
        # the unsaved edit with it. `:` returns intact, so it is left alone.
        Binding("question_mark", "help_if_saved", show=False),
    ]

    CSS = """
    FilterBrowserScreen { background: $surface; }
    FilterBrowserScreen > #settings_box {
        height: 1fr; border: round $primary 50%; padding: 0 1;
    }
    FilterBrowserScreen > #settings_box:focus-within { border: round $accent; }
    FilterBrowserScreen #filter_legend {
        height: auto; padding: 0 1; color: $text-muted; text-style: dim;
    }
    FilterBrowserScreen #filter_search {
        height: 1; padding: 0 0; border: none; background: $surface; color: $text;
    }
    FilterBrowserScreen #filter_search:focus { color: $accent; }
    /* visibility (not display) so the row is always reserved: the bar
       appearing on the first active filter must not shove the tree down.
       Same rule, glyph and position as the sidebar's. */
    FilterBrowserScreen #clear_filters_bar {
        height: 1; padding: 0 1; visibility: hidden; color: $primary 50%;
    }
    FilterBrowserScreen #clear_filters_bar:hover { color: $accent; text-style: bold; }
    FilterBrowserScreen #clear_filters_bar:focus {
        color: $accent; text-style: bold; background: $accent 15%;
    }
    FilterBrowserScreen #filter_summary {
        height: auto; max-height: 5; overflow-y: auto;
        padding: 0 1; color: $text-muted;
    }
    FilterBrowserScreen > #footer_hints {
        dock: bottom; height: 1; background: $surface; padding: 0 1; color: $text-muted;
    }
    """

    def __init__(
        self,
        *,
        title: str,
        spec: Any,
        gitignore: bool,
        fndignore: bool,
        sample_provider: Callable[[Any], Any] | None = None,
        globs: list[str] | None = None,
        excludes: list[str] | None = None,
        inherited: tuple[Any, bool, bool] | None = None,
        save_note: str = "",
        no_tags_note: str = "",
        unindexed_note: str = "",
        commit_label: str = "Save",
        on_save: Callable[[Any, bool, bool], None],
    ) -> None:
        super().__init__()
        # What this source falls back to with nothing of its own. `None` on the
        # global defaults, which inherit from nothing.
        self._inherited = inherited
        # What Ctrl+S does to the index. The two routes differ: a source save
        # reindexes its collection, the defaults save reindexes nothing.
        self._save_note = save_note
        # A branch that is simply absent reads as a missing feature, and the
        # two routes are silent for different reasons, as are "nothing is
        # indexed yet" and "indexed, and none of it is tagged".
        self._no_tags_note = no_tags_note
        self._unindexed_note = unindexed_note
        # And what it does at all. On a source this screen stages into the
        # form, which owns the write, so calling it "Save" would promise
        # something only the form does.
        self._commit_label = commit_label
        # Include globs restrict the file types too, but they cannot be shown
        # as ticked kinds: saving them back as kinds would widen a glob that
        # names one suffix of a multi-suffix type. Say so instead.
        self._globs = list(globs or ())
        # Excludes drop files before any filter runs, so a summary that names
        # only the includes is silent about half of what is skipped.
        self._excludes = list(excludes or ())
        self._title = title
        self._spec = spec
        self._gitignore = gitignore
        self._fndignore = fndignore
        self._sample: Any = None
        # Which spec the current sample was gated with. The counts are only
        # true of that one, and the screen edits the spec under them.
        self._sampled_spec: Any = None
        self._resample_timer: Any = None
        # What the screen opened with, so leaving can say whether anything is
        # being thrown away.
        self._opened_with = (spec, gitignore, fndignore)
        # A custom bound stays on offer for the visit: the radio row carrying
        # it exists only while the spec holds it, so picking a preset instead
        # would otherwise discard the value with no way back to it.
        self._kept_custom: dict[str, str] = {}
        self._sample_provider = sample_provider
        # Nothing has been sampled yet, and `_spec` is not `None`, so the
        # first `_rebuild` would schedule a scan on top of the mount one.
        self._sampled_spec = spec
        self._scanning = sample_provider is not None
        self._query = ""
        self._on_save = on_save

    def compose(self) -> ComposeResult:
        from fnd.filters.tree_model import LEGEND

        with Vertical(id="settings_box") as box:
            box.border_title = self._title
            yield Static(LEGEND, id="filter_legend")
            yield Input(placeholder=_SEARCH_PLACEHOLDER_WITH_KEY, id="filter_search")
            yield ClearFiltersBar(
                "", id="clear_filters_bar", on_clear=self.action_clear_all, focus_id="filter_tree"
            )
            yield ToggleTree("Filters", id="filter_tree")
            yield Static("", id="filter_summary")
        yield Static("", id="footer_hints")

    @on(ToggleTree.ActionSelected, "#filter_tree")
    def _on_rule_selected(self, ev: ToggleTree.ActionSelected) -> None:
        """A typed rule lives with the ticked ones, not on the screen above.

        Beside Index filters, the frontmatter rule could hold a different answer
        to the same question, with neither showing the other's.
        """
        from dataclasses import replace as _replace

        if ev.item_id.startswith(("beyond:", "rule:raw:")):
            # No picker can express these, so the row hands over to the one
            # editor that can rather than being a dead end.
            self.action_edit_text()
            return
        field_name = ev.item_id.removeprefix("rule:")
        titles = {
            "frontmatter": "Frontmatter rule · files with frontmatter",
            "expression": "Custom rule · any file",
        }
        if field_name not in titles:
            return

        def _save(text: str) -> None:
            self._spec = _replace(self._spec, **{field_name: text})
            self._rebuild()

        self.app.push_screen(
            RuleTextScreen(
                title=titles[field_name],
                value=str(getattr(self._spec, field_name, "") or ""),
                note_scoped=field_name == "frontmatter",
                on_save=_save,
            )
        )

    @on(Input.Changed, "#filter_search")
    def _on_search_changed(self, ev: Input.Changed) -> None:
        self._query = ev.value.strip().lower()
        self._rebuild(focus_tree=False)

    @on(Input.Submitted, "#filter_search")
    def _on_search_submitted(self, _ev: Input.Submitted) -> None:
        """Enter hands the rows back, with the query still narrowing them."""
        self.query_one("#filter_tree", ToggleTree).focus()

    def action_tree_from_input(self) -> None:
        """Bridge Down from the filter Input into the tree, and land on a row.

        The tree's own Down consumes the key whenever it has focus, so this
        fires only from the box.
        """
        tree = self.query_one("#filter_tree", ToggleTree)
        if tree.cursor_line < 0 and tree.root.children:
            tree.cursor_line = 0
        tree.focus()

    def action_focus_search(self) -> None:
        self.query_one("#filter_search", Input).focus()

    @on(ToggleTree.NodeHighlighted, "#filter_tree")
    def _on_row_highlighted(self, _ev: ToggleTree.NodeHighlighted[dict[str, Any]]) -> None:
        self._refresh_legend()

    def _refresh_legend(self) -> None:
        """The glyph meanings for the branch the cursor is in.

        The shared line is false on the ignore branch (● there means "obey
        this file", which indexes FEWER files), so a branch that reads
        differently says so, and the rest keep one wording.
        """
        from fnd.filters.tree_model import LEGEND

        tree = self.query_one("#filter_tree", ToggleTree)
        node = tree.cursor_node
        top: str = ""
        while node is not None and node.parent is not None:
            data = node.data if isinstance(node.data, dict) else {}
            top = str(data.get("id") or data.get("group") or top)
            node = node.parent
        branch = next(
            (
                b
                for b in getattr(self, "_branches", ())
                if top == b.id or top.startswith(b.id + ":")
            ),
            None,
        )
        self.query_one("#filter_legend", Static).update(
            (getattr(branch, "legend", "") or LEGEND) if branch is not None else LEGEND
        )

    @on(ToggleTree.NavigatedOut, "#filter_tree")
    def _on_navigated_out(self, _ev: ToggleTree.NavigatedOut) -> None:
        """← at the outermost level leaves the screen, as it does everywhere
        else in Settings. The tree's own binding would otherwise swallow it."""
        self.action_back()

    def on_mount(self) -> None:
        self._rebuild()
        self._render_footer()
        if self._sample_provider is not None:
            self.run_worker(self._load_sample, thread=True)

    def _render_footer(self) -> None:
        """The row keys are single letters, so a focused search box swallows
        them; naming them there advertises keys that do not work."""
        app: FNDApp = self.app  # type: ignore[assignment]
        typing = _typing_in(self)
        cluster: tuple[tuple[str, str], ...] = (
            (("⏎", "Rows"), ("Esc", "Clear"))
            if typing
            else (
                ("⏎", "Toggle"),
                ("→", "Open"),
                ("/", "Filter"),
                ("t", "As text"),
                *(
                    ((_CLEAR_FILTERS_KEY, "Return to defaults"),)
                    if self._can_return_to_defaults()
                    else ()
                ),
                (COMMIT_KEY, self._commit_label),
                ("y", "Copy"),
                # Esc asks; it does not discard. Naming one of the answers on
                # the key that opens the question invites Esc then Enter, which
                # loses a filter set.
                ("Esc/←", "Leave"),
            )
        )
        bar = _editor_hint_bar(cluster) if typing else _hint_bar(app, cluster)
        self.query_one("#footer_hints", Static).update(bar)

    def on_descendant_focus(self, _ev: events.DescendantFocus) -> None:
        self._render_footer()
        refresh_search_placeholder(self, "#filter_search")

    def on_descendant_blur(self, _ev: events.DescendantBlur) -> None:
        self._render_footer()
        refresh_search_placeholder(self, "#filter_search")

    def _load_sample(self) -> None:
        """Sampling opens files, so it cannot run on the event loop: the scan's
        budget is only checked between files, and one cloud-evicted note
        overruns it by as long as the provider takes to deliver."""
        wanted = self._spec
        try:
            sample = self._sample_provider(wanted) if self._sample_provider is not None else None
        except Exception:
            sample = None
        self.app.call_from_thread(self._sample_arrived, sample, wanted)

    def _sample_arrived(self, sample: Any, spec: Any = None) -> None:
        """The scan lands on a worker's schedule, so it must not move focus:
        the user may be mid-word in the row filter."""
        self._scanning = False
        self._sample = sample
        self._sampled_spec = spec
        self._rebuild(focus_tree=False)

    def _gating_spec(self, spec: Any) -> Any:
        """What the counts are actually gated with: the spec minus its kinds.

        `_sample` strips `kinds` before building the gate, so a file-type tick
        cannot change any count and must not buy a walk of the source.
        """
        import dataclasses

        if spec is None:
            return None
        return dataclasses.replace(spec, kinds=())

    def _resample_if_stale(self) -> None:
        """Re-scan when the rules on screen are not the ones the counts describe.

        Debounced: ticking through a branch changes the spec once per keypress
        and each scan walks the source. Grouped, because the mount scan is
        started directly by `on_mount` and a timer firing during it otherwise
        puts two walks in `sample_source` at once.
        """
        if self._sample_provider is None:
            return
        if self._gating_spec(self._spec) == self._gating_spec(self._sampled_spec):
            return
        if self._resample_timer is not None:
            self._resample_timer.stop()
        self._resample_timer = self.set_timer(0.3, self._start_resample)

    def _start_resample(self) -> None:
        self._resample_timer = None
        if self._gating_spec(self._spec) == self._gating_spec(self._sampled_spec):
            return
        self._scanning = True
        # Painted, or the pane shows the old counts with nothing saying they
        # are being recomputed and the flag is False again by the next repaint.
        self._refresh_summary()
        self.run_worker(self._load_sample, thread=True, exclusive=True, group="sample")

    def _rebuild(self, *, focus_tree: bool = True) -> None:
        """``focus_tree`` False where the user is typing or a worker landed.

        Focusing unconditionally meant every keystroke in the row filter moved
        focus to the tree, so the second character onwards ran as a binding:
        `/cle` reached `c`, which clears the whole set without asking.
        """
        import contextlib

        from fnd.filters.tree_model import custom_ids, selection_for, spec_branches

        self._resample_if_stale()
        tree = self.query_one("#filter_tree", ToggleTree)
        keep = tree.expanded_group_ids if tree.root.children else set()
        line = tree.cursor_line
        self._kept_custom.update(custom_ids(self._spec))
        branches = spec_branches(self._spec, self._sample, self._kept_custom)
        # Kept so a commit knows which kinds were actually on screen: ticking
        # every visible box means "all of them", not the sampled subset.
        self._branches = branches
        groups = [_branch_group(b) for b in branches]
        if self._query:
            groups = [g for g in (_matching_group(g, self._query) for g in groups) if g]
            # Everything open, or a match two levels down is still invisible.
            keep = {g.id for top in groups for g in top.walk()}
        selected, excluded = selection_for(
            self._spec, gitignore=self._gitignore, fndignore=self._fndignore
        )
        tree.set_model(groups, selected, excluded=excluded, expanded=keep)
        # The sample can land while the user is already navigating; keep them
        # where they were rather than snapping back to the first row.
        with contextlib.suppress(Exception):
            if line > 0:
                tree.cursor_line = line
        if focus_tree:
            tree.focus()
        self._update_clear_bar()
        self._refresh_legend()
        self._refresh_summary()
        self._say_when_nothing_matches(bool(groups))

    @on(ToggleTree.SelectionChanged, "#filter_tree")
    def _on_selection(self, ev: ToggleTree.SelectionChanged) -> None:
        from fnd.filters.tree_model import apply_selection, selection_for

        self._spec, self._gitignore, self._fndignore = apply_selection(
            self._spec, ev.selected, ev.excluded, self._offered_kind_ids()
        )
        # Ticking every file type IS "no rule" (the model collapses `kinds` to
        # empty), so re-derive from the spec whenever the two disagree, or
        # `● every type` stays as a state that saves nothing and returns as `○`.
        settled, settled_out = selection_for(
            self._spec, gitignore=self._gitignore, fndignore=self._fndignore
        )
        if settled != set(ev.selected) or settled_out != set(ev.excluded):
            self._rebuild(focus_tree=False)
            return
        # The ordinary tick ends here, so this is where the counts learn that
        # their rules moved. `_rebuild` is the other caller, not the only one.
        self._resample_if_stale()
        self._refresh_summary()

    def _say_when_nothing_matches(self, any_rows: bool) -> None:
        """Put an empty row filter in the pane's title.

        A blank tree that says nothing reads as a hung process. The border
        title is where this app already carries counts, so it is where the
        absence of them belongs too.
        """
        import contextlib

        with contextlib.suppress(Exception):
            box = self.query_one("#settings_box", Vertical)
            if self._query and not any_rows:
                box.border_title = f"{self._title}: no rows match {self._query!r}"
            else:
                box.border_title = self._title

    def _can_return_to_defaults(self) -> bool:
        """Whether this screen has defaults to go back to, and has left them.

        ONE predicate, read by the row, the key and the footer, so the key is
        never bound where the row is hidden: on the global set it would empty
        the shipped never-index exclusion, and the browser cannot offer that
        tag back once no file carries it.
        """
        return self._inherited is not None and (
            (self._spec, self._gitignore, self._fndignore) != self._inherited
        )

    def _update_clear_bar(self) -> None:
        """The sidebar's row, doing the index side's act.

        Search filters are ephemeral, so that row counts what it clears. These
        are config: the row restores what this source inherits, and appears
        only where the source has departed from it. The global set inherits
        from nothing, so there is nothing to return to.
        """
        bar = self.query_one("#clear_filters_bar", ClearFiltersBar)
        bar.visible = self._can_return_to_defaults()
        bar.update(RETURN_TO_DEFAULTS)

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """Textual asks this before firing a binding AND before showing it, so
        the key and the row cannot disagree about whether the act exists."""
        if action == "clear_all":
            return self._can_return_to_defaults() or None
        return True

    def _offered_kind_ids(self) -> set[str]:
        """Kind ids the tree actually showed, so "all ticked" means all of
        them rather than every id in the registry."""
        ids: set[str] = set()
        stack = list(getattr(self, "_branches", []))
        while stack:
            branch = stack.pop()
            ids |= {i[0] for i in branch.items if i[0].startswith("kind:")}
            stack.extend(branch.groups)
        return ids

    def _refresh_summary(self) -> None:
        """Show the rows as the expression they compile to.

        The text is not a separate feature to go and find: it is this filter
        set, written out, and ``t`` opens it for editing.
        """
        from fnd.filters.text_form import render

        text = render(self._spec)
        # What the expression below does NOT cover: ignore files (that they
        # apply, not which), and the walk's pruning of every dot-prefixed name,
        # lifted only for what an include glob naming a dot component matches.
        obeying = self._gitignore or self._fndignore
        head = ["obeying ignore files" if obeying else "ignore files off"]
        head.append("skipping hidden files")
        if self._globs:
            head.append("restricted to paths: " + ", ".join(self._globs))
        if self._excludes:
            head.append("skipping paths: " + ", ".join(self._excludes))
        for clash in self._spec.impossible_bounds():
            # Decidable without a corpus, and the outcome is an empty index.
            head.append(f"nothing can match: {clash}")
        if self._save_note:
            head.append(self._save_note)
        if self._query:
            head.append(f"showing rows matching {self._query!r}")
        if self._scanning:
            head.append("scanning source for types and tags…")
        elif self._unindexed_note and self._sample is None:
            head.append(self._unindexed_note)
        elif self._no_tags_note and not _any_tag(self._sample):
            head.append(self._no_tags_note)
        if self._sample is not None and self._sample.truncated:
            # Its own `if`, not the chain's last arm: the per-source browser
            # always passes a tags note, so a tagless source would never reach
            # it, and a bare row then means "none here", not "not counted".
            head.append("partial scan: this source has more types and tags")
        # Named separately because neither is a predicate over a file, so
        # neither can appear in the expression below.
        self.query_one("#filter_summary", Static).update(
            _FilterSummary(
                "Outside the expression: " + " · ".join(head),
                "expression ('t' edits, 'y' copies):  ",
                text,
            )
        )

    def _dirty(self) -> bool:
        return (self._spec, self._gitignore, self._fndignore) != self._opened_with

    def action_help_if_saved(self) -> None:
        if self._dirty():
            self.notify(f"Unsaved filter changes: {COMMIT_KEY} to save, Esc to discard, then ?")
            return
        self.app.action_show_help()  # type: ignore[attr-defined]

    def action_copy_text(self) -> None:
        """Copy the expression. The app owns the mouse, so a terminal
        selection cannot reach this text."""
        from fnd.filters.text_form import render
        from fnd.tui.clipboard import copy_text

        text = render(self._spec)
        if not text:
            self.notify("No filter expression to copy", severity="information")
            return
        try:
            copy_text(text)
            self.notify("Filter expression copied", severity="information")
        except OSError as e:
            self.notify(f"Could not copy: {e}", severity="error")

    def action_clear_all(self) -> None:
        """Return this screen's set to what it inherits.

        Emptying the resolved set instead widens the index: on a source it
        drops the inherited `no_index` exclusion, so undoing a file-type filter
        would also switch off the never-index opt-out, silently.
        """
        if not self._can_return_to_defaults():
            # The global set inherits from nothing. Emptying it here would drop
            # the shipped never-index exclusion, which is a protection rather
            # than a preference, and no row on this screen can put it back.
            return

        before = self._spec
        assert self._inherited is not None
        self._spec, self._gitignore, self._fndignore = self._inherited
        self._rebuild()
        # It takes no confirmation, so it has to say what it took, above all
        # a tag exclusion, which is a protection rather than a preference.
        self.notify(_cleared_note(before, self._spec))

    def action_edit_text(self) -> None:
        def _save(spec: Any) -> None:
            # Named when it lands, not only while typing: the text screen's
            # warning is gone by the time the tree is back.
            dropped = _protection_dropped(self._spec, spec)
            self._spec = spec
            self._rebuild()
            if dropped:
                self.app.notify(f"{dropped} files are no longer excluded", severity="warning")

        self.app.push_screen(
            FilterTextScreen(title=f"{self._title} (text)", spec=self._spec, on_save=_save)
        )

    def action_back(self) -> None:
        # Esc clears a narrowing before it leaves, as it does on the settings
        # list: leaving straight from a filtered tree loses the rows silently.
        search = self.query_one("#filter_search", Input)
        if search.value:
            search.value = ""
            self.query_one("#filter_tree", ToggleTree).focus()
            return
        _leave_or_confirm(
            self, dirty=self._dirty(), what="these filters", on_save=self.action_save_close
        )

    def unsaved_work(self) -> tuple[str, Callable[[], None]] | None:
        if not self._dirty():
            return None
        return "these filters", self.action_save_close

    def action_save_close(self) -> None:
        if not self._dirty():
            # The exit guard calls this state clean and leaves without asking;
            # saving anyway would report "Filters saved." over a byte-identical
            # config and reindex, a costly answer to a question nobody asked.
            self.app.notify("No changes to save")
            self.app.pop_screen()
            return
        try:
            self._on_save(self._spec, self._gitignore, self._fndignore)
        except Exception as e:
            self.app.notify(_summarise(e), severity="error", title="Save failed")
            return
        self.app.pop_screen()
