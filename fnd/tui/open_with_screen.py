"""Shift-O "Open with…" modal — pick which app opens the focused hit.

Surfaced when the user presses ``O`` on a result. Lists every registered
app whose ``handles`` covers the focused hit's kind (plus wildcard apps)
and is :attr:`fnd.apps.App.available`. The resolved default is
highlighted; pressing Enter fires it, letter-shortcuts dispatch other
rows directly, Esc dismisses.

Resolution order is the same as for the default ``o`` shortcut — the
chosen row gets the same :class:`fnd.apps.OpenRequest`. See the
``[app_defaults]`` section comments in the user config for the full
hierarchy.
"""

from __future__ import annotations

import string
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Static

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
        rows.sort(key=lambda r: (0 if r.is_default else 1))
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
    """ModalScreen that lists eligible apps and dispatches on selection.

    Dismiss value: the id of the app that fired (or ``None`` if the user
    pressed Esc). Lets the caller surface a status message ("Opened with
    Skim") if it wants.
    """

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "close", "Dismiss", show=True),
        Binding("enter", "fire_default", "Open with default", show=True),
    ]

    CSS = """
    OpenWithScreen {
        align: center middle;
        background: $surface 50%;
    }
    #open_with_box {
        width: 70%;
        max-width: 70;
        height: auto;
        border: round $primary;
        padding: 1 2;
        background: $surface;
    }
    #open_with_title {
        text-style: bold;
        padding-bottom: 1;
    }
    .open_with_row {
        padding: 0 1;
    }
    .open_with_row.-default {
        background: $primary 30%;
        text-style: bold;
    }
    #open_with_hint {
        color: $text-muted;
        padding-top: 1;
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
        # Register letter-shortcut bindings dynamically. Textual reads
        # BINDINGS at class level so we install per-instance bindings
        # via ``_bindings`` after super().__init__.
        for app_id, key in self._shortcuts.items():
            self._bindings.bind(key, f"fire('{app_id}')", show=False)

    def compose(self) -> ComposeResult:
        with Vertical(id="open_with_box") as box:
            box.border_title = f"Open with… ({getattr(self._hit, 'kind', '?')})"
            yield Static(
                "Pick an app for this file. Enter fires the highlighted default; "
                "letter keys dispatch directly. Resolution: per-source app → "
                "[app_defaults] → auto-default → system. See config.toml for "
                "the full hierarchy.",
                id="open_with_title",
            )
            for row in self._rows:
                key = self._shortcuts.get(row.id, "·")
                marker = "★ " if row.is_default else "  "
                line = f"[{key}] {marker}{row.display_name}"
                if row.notes:
                    line += f"  — {row.notes}"
                widget = Static(line, classes="open_with_row")
                if row.is_default:
                    widget.add_class("-default")
                yield widget
            yield Static(
                "Enter = default · letter = pick · Esc = cancel",
                id="open_with_hint",
            )

    # ── Actions ──────────────────────────────────────────────────────

    def action_close(self) -> None:
        self.dismiss(None)

    def action_fire_default(self) -> None:
        if self._default_id and self._default_id in self._registry:
            self._fire(self._default_id)
            return
        # No default → pick the first row if any.
        if self._rows:
            self._fire(self._rows[0].id)

    def action_fire(self, app_id: str) -> None:
        self._fire(app_id)

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
        hit = self._hit
        source = self._source
        params: dict[str, str] = {}
        source_path: Path | None = None
        if source is not None:
            params = dict(getattr(source, "app_params", {}) or {})
            source_path = getattr(source, "path", None)
        path = Path(str(getattr(hit, "path", "")))
        return OpenRequest(
            path=path,
            kind=str(getattr(hit, "kind", "")),
            page=int(getattr(hit, "page", 0) or 0),
            slide=int(getattr(hit, "slide", 0) or 0),
            heading_path=str(getattr(hit, "heading_path", "") or ""),
            line=int(getattr(hit, "line", 0) or 0),
            query=str(getattr(hit, "query", "") or ""),
            vault=params.get("vault", ""),
            file_in_vault=_relative_or_empty(path, source_path),
            source_path=source_path,
        )


def _relative_or_empty(target: Path, root: Path | None) -> str:
    if root is None:
        return ""
    try:
        return str(target.expanduser().resolve().relative_to(Path(root).expanduser().resolve()))
    except (ValueError, OSError):
        return ""
