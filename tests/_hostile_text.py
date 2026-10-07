"""Text that has broken a sink, shared by every sink's and boundary's tests."""

from __future__ import annotations

MARKUP: tuple[str, ...] = (
    "close [/] tag",  # MarkupError: nothing to close
    "Q3 [growth=5%]",  # MarkupError: bad style value
    "Invoice [PAID].pdf",  # eaten by Content, and escape() does not stop it
    "theme [$accent].md",  # eaten as a variable
    "[@click=app.quit]x[/]",  # an action link
    "x [link=file:///etc]y[/link]",  # MarkupError
    r"C:\notes\[draft]\a.md",  # escape() doubles the backslash
)

TERMINAL: tuple[str, ...] = (
    "\x1b]52;c;aGVsbG8=\x07clip",  # OSC 52 clipboard write
    "\x1b]8;;https://evil/\x07link\x1b]8;;\x07",  # OSC 8 hyperlink
    "a\x1b[2Kb",  # CSI erase line
    "tab\there",
    "new\nline",
    "cr\rreturn",
    "bidi\u202eevil",
    "zero\u200bwidth",
)

ALL: tuple[str, ...] = MARKUP + TERMINAL
