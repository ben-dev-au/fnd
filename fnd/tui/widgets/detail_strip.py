"""DetailStrip — 2-line description + metadata area at the bottom of
every settings screen.

Empty by default. The parent screen calls ``set(description, metadata)``
on cursor row changes; ``clear()`` blanks it. Uses Rich Text so the
metadata line gets $text-muted styling and the description line stays
plain $text.
"""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.widget import Widget
from textual.widgets import Static


class DetailStrip(Widget):
    """A two-line dim area docked at the bottom of a settings container.

    Line 1: row description in $text.
    Line 2: metadata (storage path, range, applicability note) in $text-muted.
    Separated from the row list above by the container's own thin rule.
    """

    DEFAULT_CSS = """
    /* Top horizontal rule visually separates guidance from the row list
       above. The DetailStrip already sits inside the settings_box's
       round border, so a full box here would double-bracket the panel.

       Height is ``auto`` so the description wraps onto as many lines as
       it needs without truncating. ``max-height: 6`` bounds the panel
       so a runaway-long description can't dominate the screen — keep
       descriptions concise enough to fit comfortably. */
    DetailStrip {
        height: auto;
        max-height: 6;
        padding: 0 0 0 0;
        border-top: hkey $primary 50%;
    }
    DetailStrip > Static { padding: 0 1; }
    DetailStrip > Static.-description { color: $text; height: auto; }
    DetailStrip > Static.-metadata { color: $text-muted; height: 1; }
    """

    def __init__(self) -> None:
        super().__init__()
        self._description: str = ""
        self._metadata: str = ""
        # Whether the current description is trusted Rich markup. Off by
        # default so arbitrary text (paths, globs, app notes) renders
        # literally — only opted-in rows colour their description.
        self._description_is_markup: bool = False

    def compose(self) -> ComposeResult:
        yield Static("", classes="-description", id="detail_description")
        yield Static("", classes="-metadata", id="detail_metadata")

    def set(self, description: str, metadata: str = "", *, markup: bool = False) -> None:
        self._description = description
        self._metadata = metadata
        self._description_is_markup = markup
        self._refresh_strip()

    def clear(self) -> None:
        self.set("", "")

    def _refresh_strip(self) -> None:
        rendered = self._render_lines()
        try:
            self.query_one("#detail_description", Static).update(rendered[0])
            self.query_one("#detail_metadata", Static).update(rendered[1])
        except Exception:
            pass

    def _render_lines(self) -> tuple[Text, Text]:
        """Pure render — tested directly without mounting the widget."""
        if not self._description:
            description = Text("")
        elif self._description_is_markup:
            description = self._markup(self._description)
        else:
            description = Text(self._description)
        return (
            description,
            Text(self._metadata, style="dim") if self._metadata else Text(""),
        )

    @staticmethod
    def _markup(text: str) -> Text:
        """Render an opted-in description as Rich markup so toggles can
        colour their effects (e.g. ``[green]+[/]`` / ``[red]-[/]``). Falls
        back to literal text if the markup is malformed."""
        try:
            return Text.from_markup(text)
        except Exception:
            return Text(text)

    def on_mount(self) -> None:
        self._refresh_strip()
