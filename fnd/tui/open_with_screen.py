"""Shift-O "Open with…" modal — pick which app opens the focused hit.

Surfaced when the user presses ``O`` on a result. One job: let the
user pick an app for this file and fire it. The picker is a plain
list of "<key>  <marker> <name>" rows:

* Arrow / j / k move the cursor.
* Enter fires the cursor's app.
* The resolved default (what `o` would fire) starts under the cursor
  and is marked with ★.
* Letter shortcuts (first unique letter of the display name) fire any
  row directly without moving the cursor.
* Esc dismisses.

No descriptions, hierarchy explainers, or app notes live here —
that's settings territory. Resolution itself is exactly what `o` uses;
see ``[app_defaults]`` in config.toml for the rules.
"""

from __future__ import annotations

import string
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option

from fnd.apps import App, OpenRequest

# ── Pure helpers (testable without Pilot) ─────────────────────────────


@dataclass(frozen=True)
class OpenWithRow:
    """One row in the modal."""

    id: str
    display_name: str
    notes: str
    is_default: bool


def eligible_apps(
    *,
    kind: str,
    registry: Mapping[str, App],
    availability: Mapping[str, bool] | None = None,
    default_id: str | None = None,
) -> list[OpenWithRow]:
    """Filter ``registry`` to apps that handle ``kind`` (or wildcard) and
    are available, then sort: resolved default first, then registry
    insertion order.

    ``availability`` overrides the live ``app.available()`` probe — used
    by tests so we don't need to mock every individual ``_*_exists``
    helper.
    """
    rows: list[OpenWithRow] = []
    for app_id, app in registry.items():
        if kind not in app.handles and "*" not in app.handles:
            continue
        is_avail = (
            availability[app_id] if availability and app_id in availability else app.available()
        )
        if not is_avail:
            continue
        rows.append(
            OpenWithRow(
                id=app_id,
                display_name=app.display_name,
                notes=app.notes,
                is_default=(app_id == default_id),
            )
        )
    if default_id:
        rows.sort(key=lambda r: 0 if r.is_default else 1)
    return rows


_LETTER_FALLBACK_DIGITS: tuple[str, ...] = tuple("123456789")


def letter_shortcuts(rows: Iterable[Any]) -> dict[str, str]:
    """Assign a unique single-character shortcut to each row.

    Strategy: walk the display_name letters (lowercased a-z) in order
    and pick the first unused one. If every letter in the name is
    already taken, fall back to a digit (1, 2, …). Returns a mapping
    from ``row.id`` → key.
    """
    used: set[str] = set()
    out: dict[str, str] = {}
    digit_idx = 0
    for row in rows:
        chosen: str | None = None
        for ch in row.display_name.lower():
            if ch in string.ascii_lowercase and ch not in used:
                chosen = ch
                break
        if chosen is None:
            while digit_idx < len(_LETTER_FALLBACK_DIGITS):
                ch = _LETTER_FALLBACK_DIGITS[digit_idx]
                digit_idx += 1
                if ch not in used:
                    chosen = ch
                    break
        if chosen is None:
            continue  # 9+ rows past digit range; skip — modal cap
        used.add(chosen)
        out[row.id] = chosen
    return out


# ── Modal screen ──────────────────────────────────────────────────────


class OpenWithScreen(ModalScreen[str | None]):
    """Minimal app picker. Dismiss value: the fired app id, or ``None``
    if Esc was pressed."""

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape,q", "close", "Cancel", show=False),
    ]

    CSS = """
    OpenWithScreen {
        align: center middle;
        background: $surface 50%;
    }
    #open_with_box {
        width: auto;
        max-width: 60;
        min-width: 32;
        height: auto;
        border: round $primary;
        padding: 0 1;
        background: $surface;
    }
    #open_with_list {
        height: auto;
        background: $surface;
        border: none;
        padding: 0;
    }
    #open_with_hint {
        color: $text-muted;
        padding: 1 1 0 1;
    }
    """

    def __init__(
        self,
        *,
        hit: Any,
        source: Any | None,
        registry: Mapping[str, App],
        default_id: str | None,
    ) -> None:
        super().__init__()
        self._hit = hit
        self._source = source
        self._registry = registry
        self._default_id = default_id
        self._rows = eligible_apps(
            kind=getattr(hit, "kind", ""),
            registry=registry,
            default_id=default_id,
        )
        self._shortcuts = letter_shortcuts(self._rows)
        # Per-instance letter-shortcut bindings. ``show=False`` keeps
        # them out of the chrome — they're visible inline next to each
        # row already.
        for app_id, key in self._shortcuts.items():
            self._bindings.bind(key, f"fire('{app_id}')", show=False)

    def compose(self) -> ComposeResult:
        kind = getattr(self._hit, "kind", "?")
        with Vertical(id="open_with_box") as box:
            box.border_title = f" Open with — .{kind} "
            options: list[Option] = []
            for row in self._rows:
                options.append(Option(self._row_text(row), id=row.id))
            yield OptionList(*options, id="open_with_list")
            yield Static(
                "↑↓ pick · Enter open · letter jump · Esc cancel",
                id="open_with_hint",
            )

    def _row_text(self, row: OpenWithRow) -> Text:
        """One row's display. Use ``rich.text.Text`` (not a markup
        string) so characters like ``[s]`` are rendered literally
        instead of being interpreted as Rich style tags."""
        key = self._shortcuts.get(row.id, " ")
        marker = "★" if row.is_default else " "
        text = Text()
        text.append(f" {key} ", style="bold")
        text.append(f" {marker} ", style="yellow" if row.is_default else "")
        text.append(row.display_name)
        if row.is_default:
            text.stylize("bold")
        return text

    def on_mount(self) -> None:
        lst = self.query_one("#open_with_list", OptionList)
        # Park the cursor on the resolved default so Enter does the
        # same thing as `o` would. Walks the populated option list
        # because OptionList doesn't expose a "highlight by id" API.
        if self._default_id:
            for idx, opt in enumerate(lst.options):
                if opt.id == self._default_id:
                    lst.highlighted = idx
                    break
        lst.focus()

    # ── Actions ──────────────────────────────────────────────────────

    def action_close(self) -> None:
        self.dismiss(None)

    def action_fire(self, app_id: str) -> None:
        self._fire(app_id)

    @on(OptionList.OptionSelected, "#open_with_list")
    def _on_option_selected(self, ev: OptionList.OptionSelected) -> None:
        if ev.option.id:
            self._fire(ev.option.id)

    def _fire(self, app_id: str) -> None:
        app = self._registry.get(app_id)
        if app is None:
            self.dismiss(None)
            return
        req = self._build_request()
        try:
            rc = app.handler(req)
        except Exception as exc:  # surface but don't crash the modal
            self.app.notify(f"Open failed: {exc}", title=app.display_name, severity="error")
            self.dismiss(None)
            return
        if rc != 0:
            self.app.notify(
                f"{app.display_name} returned exit code {rc}",
                title="Open",
                severity="warning",
            )
        self.dismiss(app_id)

    def _build_request(self) -> OpenRequest:
        from fnd.apps import detect_obsidian_vault_path

        hit = self._hit
        source = self._source
        params: dict[str, str] = {}
        source_path: Path | None = None
        if source is not None:
            params = dict(getattr(source, "app_params", {}) or {})
            source_path = getattr(source, "path", None)
        path = Path(str(getattr(hit, "path", "")))
        # Match opener.open_smart: when a vault is configured, anchor
        # file_in_vault on the actual vault root (the dir containing
        # .obsidian/) so deep-links work even when source.path is a
        # subdirectory of the vault.
        vault_root = detect_obsidian_vault_path(path) if params.get("vault") else None
        return OpenRequest(
            path=path,
            kind=str(getattr(hit, "kind", "")),
            page=int(getattr(hit, "page", 0) or 0),
            page_label=str(getattr(hit, "page_label", "") or ""),
            slide=int(getattr(hit, "slide", 0) or 0),
            heading_path=str(getattr(hit, "heading_path", "") or ""),
            line=int(getattr(hit, "line", 0) or 0),
            query=str(getattr(hit, "query", "") or ""),
            vault=params.get("vault", ""),
            file_in_vault=_relative_or_empty(path, vault_root or source_path),
            source_path=source_path,
        )


def _relative_or_empty(target: Path, root: Path | None) -> str:
    if root is None:
        return ""
    try:
        return str(target.expanduser().resolve().relative_to(Path(root).expanduser().resolve()))
    except (ValueError, OSError):
        return ""
