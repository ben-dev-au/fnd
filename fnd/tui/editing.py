"""The navigation language every Settings surface speaks.

Each editing screen declares one role. A setting applies the moment it changes,
so leaving is all there is to do. A document holds fields that are valid
together: ^s writes it to config and indexes nothing, and leaving it unsaved
asks once. A part is opened from a document and edits it: leaving hands its
edits back, and only an invalid typed value holds the user there. Footers, the
prompt and the Keybindings sheet take their words from here.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any, ClassVar

from textual.binding import Binding
from textual.screen import Screen

from fnd.tui.widgets import COMMIT_KEY


class Role(Enum):
    SETTING = "setting"
    DOCUMENT = "document"
    PART = "part"
    CONFIRM = "confirm"
    VIEW = "view"


BACK = "Back"
CLEAR = "Clear"
CANCEL = "Cancel"
SET = "Set"
SAVE = "Save"
KEEP_EDITING = "Keep editing"
SAVE_KEY = COMMIT_KEY


def leave_hint(*, clearing: bool = False, with_left: bool = True) -> tuple[str, str]:
    """Esc (and ← where it leaves): Clear while a row filter has text, else Back."""
    return ("Esc/←" if with_left else "Esc", CLEAR if clearing else BACK)


def save_hint() -> tuple[str, str]:
    return (SAVE_KEY, SAVE)


def typing_hints() -> tuple[tuple[str, str], ...]:
    return (("⏎", SET), ("Esc", CANCEL))


@dataclass(frozen=True)
class Leave:
    """Where the user asked to go; the prompt's options name it."""

    verb: str
    run: Callable[[], object]


class DocumentScreen(Screen[None]):
    """Fields saved together with ^s; leaving with unsaved changes asks once."""

    ROLE: ClassVar[Role] = Role.DOCUMENT
    SUBJECT: ClassVar[str] = "this document"
    BINDINGS = [  # noqa: RUF012
        Binding("ctrl+s", "save", show=False),
        Binding("escape,left", "back", show=False),
    ]

    def is_dirty(self) -> bool:
        raise NotImplementedError

    def blocked_reason(self) -> str:
        return ""

    def write(self) -> str:
        """Write the working copy to config: "" on success, else why not. Never navigates."""
        raise NotImplementedError

    def after_save(self) -> None:
        """Say what the save left out of date (spec D4)."""

    def land_typing(self, resume: Callable[[], None]) -> bool:
        """Commit an open edit bar first; True means ``resume`` runs once it lands."""
        return False

    def show_refusal(self, reason: str) -> None:
        self.app.notify(reason, severity="error", timeout=6)

    def save(self) -> str:
        reason = self.blocked_reason() or self.write()
        if reason:
            self.show_refusal(reason)
            return reason
        self.after_save()
        return ""

    def action_save(self) -> None:
        if self.land_typing(self.action_save):
            return
        if not self.is_dirty():
            self.app.notify("No changes to save")
            self.app.pop_screen()
            return
        if not self.save():
            self.app.pop_screen()

    def request_leave(self) -> None:
        if not self.is_dirty():
            self.app.pop_screen()
            return
        ask_before_leaving(self, Leave("go back", self.app.pop_screen))

    def action_back(self) -> None:
        self.request_leave()


class PartScreen(Screen[None]):
    """Edits its document; Esc hands the edits back, holding only on an invalid value."""

    ROLE: ClassVar[Role] = Role.PART
    BINDINGS = [Binding("escape", "back", show=False)]  # noqa: RUF012

    _refused: str = ""

    def hand_back(self) -> str:
        """Carry the edits into the document: "" on success, else why not."""
        return ""

    def abandon(self) -> None:
        """Drop the typing the user chose not to fix."""

    def on_refused(self) -> None:
        """Repaint the footer: Esc now cancels."""

    def show_refusal(self, reason: str) -> None:
        self.app.notify(
            f"{reason}. Fix it, or press Esc again to cancel the typing.",
            severity="error",
            timeout=6,
        )

    @property
    def refused(self) -> bool:
        return bool(self._refused)

    def edited(self) -> None:
        """New typing re-arms the hold, so a fixed value is carried back."""
        if self._refused:
            self._refused = ""
            self.on_refused()

    def request_leave(self) -> None:
        if self._refused:
            self.abandon()
            self.app.pop_screen()
            return
        reason = self.hand_back()
        if reason:
            self._refused = reason
            self.show_refusal(reason)
            self.on_refused()
            return
        self.app.pop_screen()

    def action_back(self) -> None:
        self.request_leave()


def pending_document(stack: Sequence[Screen[Any]]) -> tuple[DocumentScreen, str] | None:
    """The topmost document that leaving would lose work from, once every part
    above it has handed back, and why it cannot be saved ("" when it can)."""
    held = ""
    for screen in reversed(list(stack)):
        if isinstance(screen, PartScreen):
            held = held or screen.hand_back()
        elif isinstance(screen, DocumentScreen):
            if held or screen.is_dirty():
                return screen, held or screen.blocked_reason()
            # A clean document can sit on a dirty one (a shortcut run from the help).
            held = ""
    return None


def ask_before_leaving(
    document: DocumentScreen, leave: Leave, *, blocked: str | None = None
) -> None:
    """The one unsaved-changes prompt: Save and <verb>, Discard and <verb>, Keep editing."""
    from fnd.tui.settings_screen import UnsavedChangesScreen

    reason = document.blocked_reason() if blocked is None else blocked

    def _save_then_leave() -> None:
        if not document.save():
            leave.run()

    document.app.push_screen(
        UnsavedChangesScreen(
            subject=document.SUBJECT,
            verb=leave.verb,
            on_save=None if reason else _save_then_leave,
            on_discard=leave.run,
            blocked=reason,
        )
    )


def editing_help_rows() -> tuple[tuple[str, str, str, str], ...]:
    """The Keybindings sheet's Editing section, in the words the footers use."""
    return (
        (
            "⏎",
            "Toggle / Select / Edit",
            "",
            "A setting applies the moment it changes; there is nothing to save.",
        ),
        (
            "Esc / ←",
            BACK,
            "",
            "Go back a screen. Changes go with you: settings are already applied, "
            "and a part carries its edits into the form it came from. A form with "
            "unsaved changes asks first.",
        ),
        (
            "Esc / ←",
            CLEAR,
            "",
            "With text in a row filter, empties it first; the next press goes back.",
        ),
        (
            SAVE_KEY,
            SAVE,
            "",
            "Only on a form (a source, Add collection, Index filters): writes it to "
            "the config. Saving never indexes; a collection that is then out of "
            "date carries ↻, and u brings it up to date.",
        ),
        ("⏎ (typing)", SET, "", "Take the typed value."),
        (
            "Esc (typing)",
            CANCEL,
            "",
            "Abandon the value being typed, or a confirm dialog's action.",
        ),
    )
