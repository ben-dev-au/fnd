"""Confirm a large whole-file warm before spending the cache on it.

What a whole-file warm costs cannot be predicted from anything known before it
starts: measured across eight real files, time per chunk varies 6x and capture
bytes per chunk 9x. A size gate would therefore be wrong by an order of
magnitude on some files, so the estimate goes to the user with the decision.
"""

from __future__ import annotations

from typing import Any, ClassVar

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Static

from fnd.tui.preview import tuning

__all__ = ["FullWarmConfirmScreen", "estimate_capture_mb"]


def estimate_capture_mb(chars: int) -> float:
    """Roughly what capturing ``chars`` of chunk text will hold in the cache."""
    return chars / 1000.0 * tuning.FULL_WARM_KB_PER_1K_CHARS / 1000.0


class FullWarmConfirmScreen(ModalScreen[bool]):
    """Disclosure before warming a big file whole. ``dismiss(True)`` proceeds."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "cancel", "Cancel", show=False),
        Binding("up,k", "cursor(-1)", show=False),
        Binding("down,j", "cursor(1)", show=False),
        Binding("enter", "activate", show=False),
    ]

    CSS = """
    FullWarmConfirmScreen { align: center middle; background: $surface 75%; }
    #full_warm_box {
        width: auto; min-width: 60; max-width: 100;
        height: auto; max-height: 90%;
        border: round $warning; padding: 0 1; background: $surface;
    }
    #full_warm_body { padding: 0 0 1 0; }
    #full_warm_list { height: auto; }
    """

    def __init__(self, *, name: str, chunks: int, chars: int) -> None:
        super().__init__()
        self._file_name = name
        self._chunks = chunks
        self._chars = chars

    def compose(self) -> ComposeResult:
        from rich.text import Text
        from textual.widgets import OptionList
        from textual.widgets.option_list import Option

        from fnd.tui.settings_screen import build_confirm_body

        mb = estimate_capture_mb(self._chars)
        body = build_confirm_body(
            outcome_label="What",
            outcome=(
                f"Captures all {self._chunks} sections of '{self._file_name}' so "
                f"scrolling anywhere in it is instant, not just its matches."
            ),
            cost_label="Cache",
            cost=(
                f"About {mb:.0f} MB. Large files can push other warmed files "
                f"out of the cache, which then have to warm again."
            ),
            safety_label="Background",
            safety="Keep working while it runs. Press w again on this file to stop it.",
        )
        with Vertical(id="full_warm_box") as box:
            box.border_title = "Warm this file completely?"
            yield Static(body, id="full_warm_body")
            yield OptionList(
                Option(Text("Warm it", style="bold green"), id="warm"),
                Option("Cancel", id="cancel"),
                id="full_warm_list",
            )

    def on_mount(self) -> None:
        from textual.widgets import OptionList

        self.query_one("#full_warm_list", OptionList).focus()

    def action_cursor(self, direction: int) -> None:
        from textual.widgets import OptionList

        lst = self.query_one("#full_warm_list", OptionList)
        if direction > 0:
            lst.action_cursor_down()
        else:
            lst.action_cursor_up()

    def action_activate(self) -> None:
        from textual.widgets import OptionList

        self.query_one("#full_warm_list", OptionList).action_select()

    def action_cancel(self) -> None:
        self.dismiss(False)

    def on_option_list_option_selected(self, ev: Any) -> None:
        self.dismiss(ev.option.id == "warm")
