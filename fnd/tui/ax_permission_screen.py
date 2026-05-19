"""Modal explaining why a handler needs macOS Accessibility permission.

Triggered the first time an AX-gated path (today: the Preview page-jump
AppleScript handler) detects a denied state. The modal:

* Explains what the user tried, what's missing, and why we need it.
* Offers a one-click deep-link into
  ``System Settings → Privacy & Security → Accessibility`` via the
  ``x-apple.systempreferences:`` URL scheme.
* Provides a "Try again" affordance that clears
  :func:`fnd.apps._reset_ax_cache` so the next open can retry without
  restarting the TUI.

Once dismissed, future denials in the same session fall back to the
quiet :meth:`textual.app.App.notify` path so we don't pop the modal on
every keystroke.
"""

from __future__ import annotations

import subprocess
from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static

# macOS Settings URL — opens straight to the Accessibility privacy pane
# (Sequoia and earlier). Stable since macOS 13.
_SETTINGS_URL = "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"


class AccessibilityPermissionScreen(ModalScreen[None]):
    """Modal with an "Open System Settings" deep-link and a "Try again" button."""

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape,q", "close", "Dismiss", show=True),
        Binding("o", "open_settings", "Open System Settings", show=True),
        Binding("r", "retry", "Try again", show=True),
    ]

    CSS = """
    AccessibilityPermissionScreen {
        align: center middle;
        background: $surface 50%;
    }
    #ax_modal {
        width: 80%;
        max-width: 80;
        height: auto;
        border: round $warning;
        padding: 1 2;
        background: $surface;
    }
    #ax_title {
        color: $warning;
        text-style: bold;
        padding-bottom: 1;
    }
    #ax_body {
        padding-bottom: 1;
    }
    #ax_steps {
        background: $panel;
        padding: 1 2;
        margin-bottom: 1;
        border: round $primary 50%;
    }
    #ax_buttons {
        height: 3;
        align: center middle;
    }
    #ax_buttons Button {
        margin: 0 1;
    }
    #ax_hint {
        padding-top: 1;
        color: $text-muted;
    }
    """

    def __init__(self, *, action_desc: str = "open the file at its match position") -> None:
        super().__init__()
        # What the user was trying to do — flows into the modal copy so the
        # message reads naturally regardless of which AX-gated path tripped.
        self._action_desc = action_desc

    def compose(self) -> ComposeResult:
        with Vertical(id="ax_modal"):
            yield Static("Accessibility permission needed", id="ax_title")
            yield Static(
                f"fnd just tried to {self._action_desc}, but macOS blocked the "
                "automation step because the app that launched fnd isn't in "
                "Accessibility. The file still opens — you just won't jump to "
                "the right page until permission is granted.",
                id="ax_body",
            )
            yield Static(
                "1. Press 'o' (or click below) to open System Settings.\n"
                "2. Find the app you launched fnd from (Terminal, iTerm, "
                "VS Code, etc.) and toggle it on.\n"
                "3. Press 'r' (or click Try again) — no need to restart fnd.",
                id="ax_steps",
            )
            with Horizontal(id="ax_buttons"):
                yield Button("Open System Settings", id="ax_open_btn", variant="primary")
                yield Button("Try again", id="ax_retry_btn")
                yield Button("Dismiss", id="ax_dismiss_btn")
            yield Static(
                "Tip: macOS asks once per app. After granting, you won't see "
                "this dialog again for that launcher.",
                id="ax_hint",
            )

    # ── Actions ──────────────────────────────────────────────────────

    def action_open_settings(self) -> None:
        # `open <url>` returns immediately; the settings pane comes to the
        # foreground over the terminal hosting fnd. Popen so we don't block
        # if `open` is slow on first use.
        subprocess.Popen(
            ["open", _SETTINGS_URL],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def action_retry(self) -> None:
        from fnd import apps

        apps._reset_ax_cache()
        self.app.notify(
            "Accessibility cache cleared. Press 'o' on the result again to retry.",
            title="Try again",
            timeout=4,
        )
        self.dismiss(None)

    def action_close(self) -> None:
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id
        if button_id == "ax_open_btn":
            self.action_open_settings()
        elif button_id == "ax_retry_btn":
            self.action_retry()
        elif button_id == "ax_dismiss_btn":
            self.action_close()
