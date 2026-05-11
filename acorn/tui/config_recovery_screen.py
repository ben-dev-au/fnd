"""Config recovery flow.

Shown when ``acorn.config.load()`` raises at TUI startup — i.e. when the
on-disk ``config.toml`` is unparseable or fails Pydantic validation. The
user is offered three deliberate actions (open in editor, reset to
defaults with a timestamped backup, exit) instead of seeing a crash.

The flow is implemented as a standalone Textual ``App`` rather than a
Screen inside ``AcornApp`` so the main app's ``__init__`` (which needs a
valid config) never has to deal with a half-valid state.
"""

from __future__ import annotations

import datetime as _dt
import os
import subprocess
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import ValidationError
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import Static


def _format_error(exc: Exception, config_path: Path) -> str:
    """Render a load() failure as the body of the recovery screen."""
    if isinstance(exc, tomllib.TOMLDecodeError):
        return f"TOML parse error in {config_path}:\n\n  {exc}"
    if isinstance(exc, ValidationError):
        lines = [f"Schema validation error in {config_path}:", ""]
        for err in exc.errors():
            loc = ".".join(str(x) for x in err["loc"]) or "(root)"
            lines.append(f"  {loc}: {err['msg']}")
        return "\n".join(lines)
    return f"Failed to load {config_path}:\n\n  {exc}"


def _backup_name(config_path: Path) -> Path:
    stamp = _dt.datetime.now(tz=_dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    return config_path.with_name(f"{config_path.name}.bak-{stamp}")


class _ResetConfirmScreen(Screen[bool]):
    """y/N confirm modal for the destructive reset action."""

    BINDINGS = [  # noqa: RUF012
        Binding("y,Y", "yes", "Yes", show=True),
        Binding("n,N,escape", "no", "No", show=True),
    ]

    CSS = """
    _ResetConfirmScreen { align: center middle; background: $surface 80%; }
    #reset_confirm_box {
        width: 70%; height: auto;
        border: round $error;
        padding: 1 2; background: $surface;
    }
    #reset_confirm_box Static { padding: 0 0 1 0; }
    .footer_hint { color: $text-muted; padding-top: 1; }
    """

    def __init__(self, config_path: Path) -> None:
        super().__init__()
        self._config_path = config_path

    def compose(self) -> ComposeResult:
        yield Vertical(
            Static("[bold]Reset config?[/]"),
            Static(
                f"This will move your current file to a backup\n"
                f"  {_backup_name(self._config_path).name}\n"
                f"and write a fresh template at\n"
                f"  {self._config_path.name}"
            ),
            Static("[y] yes   [N/Esc] no", classes="footer_hint"),
            id="reset_confirm_box",
        )

    def action_yes(self) -> None:
        self.dismiss(True)

    def action_no(self) -> None:
        self.dismiss(False)


class ConfigRecoveryScreen(Screen["Literal['valid', 'exit']"]):
    """Reusable recovery flow as a Textual ``Screen``.

    Three keyed actions:
      1 / e   Open the file in ``$EDITOR``; re-validate on return.
      2 / r   Reset to defaults (current file is renamed to a timestamped
              backup; a fresh ``CONFIG_TEMPLATE`` is written in its place).
      3 / q   Dismiss (back to caller). At TUI startup the standalone
              :class:`ConfigRecoveryApp` exits the process; in-session
              the main app stays open and the user lands back where they
              were.

    Returns ``"valid"`` if the recovery succeeded and ``"exit"`` if the
    user backed out without fixing the file.
    """

    BINDINGS = [  # noqa: RUF012
        Binding("1,e", "open_editor", "Open in editor", show=True),
        Binding("2,r", "reset", "Reset to defaults", show=True),
        Binding("3,q,escape", "dismiss_screen", "Dismiss", show=True),
    ]

    CSS = """
    ConfigRecoveryScreen { align: center middle; background: $surface; }
    #recovery_box {
        width: 90%; max-width: 100;
        height: auto;
        border: round $error;
        padding: 1 2;
        background: $surface;
    }
    #recovery_title { color: $error; text-style: bold; padding-bottom: 1; }
    #recovery_intro { padding-bottom: 1; }
    #recovery_error {
        background: $panel;
        color: $text;
        padding: 1 2;
        margin-bottom: 1;
        border: round $primary 50%;
    }
    .recovery_choice { padding: 0 0 0 0; }
    #recovery_hints { padding-top: 1; color: $text-muted; }
    """

    def __init__(self, *, error_text: str, config_path: Path) -> None:
        super().__init__()
        self._error_text = error_text
        self._config_path = config_path

    def compose(self) -> ComposeResult:
        with Vertical(id="recovery_box"):
            yield Static("Acorn could not load your config.", id="recovery_title")
            yield Static(
                f"File: {self._config_path}\n" "Pick an action below to fix or reset it.",
                id="recovery_intro",
            )
            yield Static(self._error_text, id="recovery_error")
            yield Static("[1] Open in $EDITOR", classes="recovery_choice")
            yield Static(
                "[2] Reset to defaults (current file backed up)", classes="recovery_choice"
            )
            yield Static("[3] Dismiss", classes="recovery_choice")
            yield Static(
                "Press 1, 2, or 3 — or e / r / q.",
                id="recovery_hints",
            )

    # ── Actions ──────────────────────────────────────────────────────

    def action_open_editor(self) -> None:
        editor = os.environ.get("EDITOR") or os.environ.get("VISUAL") or "vi"
        self._config_path.parent.mkdir(parents=True, exist_ok=True)
        if not self._config_path.exists():
            from acorn.config import CONFIG_TEMPLATE

            self._config_path.write_text(CONFIG_TEMPLATE, encoding="utf-8")
        with self.app.suspend():
            subprocess.call([editor, str(self._config_path)])
        try:
            from acorn.config import load

            load(self._config_path)
        except Exception as e:
            self._error_text = _format_error(e, self._config_path)
            self.query_one("#recovery_error", Static).update(self._error_text)
            return
        self.dismiss("valid")

    def action_reset(self) -> None:
        self.app.push_screen(
            _ResetConfirmScreen(self._config_path), callback=self._on_reset_confirmed
        )

    def _on_reset_confirmed(self, confirmed: bool | None) -> None:
        if not confirmed:
            return
        from acorn.config import CONFIG_TEMPLATE, load

        backup = _backup_name(self._config_path)
        try:
            if self._config_path.exists():
                self._config_path.rename(backup)
            self._config_path.parent.mkdir(parents=True, exist_ok=True)
            self._config_path.write_text(CONFIG_TEMPLATE, encoding="utf-8")
            load(self._config_path)
        except Exception as e:
            self._error_text = f"Reset failed: {e}"
            self.query_one("#recovery_error", Static).update(self._error_text)
            return
        if backup.exists():
            self.app.notify(
                f"Reset done. Backup at {backup.name}",
                title="Config",
                timeout=5,
            )
        self.dismiss("valid")

    def action_dismiss_screen(self) -> None:
        self.dismiss("exit")


class ConfigRecoveryApp(App[None]):
    """Standalone wrapper used at TUI startup before the main app exists.

    Behaviour is unchanged from Phase 1 — pushes the reusable
    :class:`ConfigRecoveryScreen` and exits when the user resolves it.
    """

    CSS = """
    Screen { align: center middle; background: $surface; }
    """

    def __init__(self, *, error_text: str, config_path: Path) -> None:
        super().__init__()
        self._error_text = error_text
        self._config_path = config_path
        self.resolution: Literal["valid", "exit"] = "exit"

    def on_mount(self) -> None:
        self.push_screen(
            ConfigRecoveryScreen(error_text=self._error_text, config_path=self._config_path),
            callback=self._on_done,
        )

    def _on_done(self, result: str | None) -> None:
        self.resolution = "valid" if result == "valid" else "exit"
        self.exit(None)


def run_recovery(error: Exception, config_path: Path) -> bool:
    """Run the recovery flow synchronously. Returns ``True`` if the user
    produced a valid config (caller should re-attempt ``Config.load()``),
    ``False`` if they elected to exit.
    """
    error_text = _format_error(error, config_path)
    app = ConfigRecoveryApp(error_text=error_text, config_path=config_path)
    app.run()
    return app.resolution == "valid"
