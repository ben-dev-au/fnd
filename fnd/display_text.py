"""Make arbitrary text safe to write to a terminal: two policies, one per sink.

File names, headings, extracted body text, config values and error messages
reach the screen from sources fnd does not control. ``ESC``, ``BEL`` and the C1
controls start escape sequences that can write the clipboard or forge a link,
``\\r`` overwrites a line, and bidi controls reorder what the reader sees.

* ``display_*`` fits a fixed-width TUI cell: a raw ``\\t`` measures zero cells
  yet a terminal expands it, shearing a bordered row, so every separator becomes
  one space and every control or format character is dropped. The TUI reaches
  it through :mod:`fnd.tui.ui_text`.
* ``terminal_*`` is for CLI text a user reads, copies or pipes: everything
  printable is kept verbatim (a path must stay the real path), and only what
  acts on the terminal is shown, as a visible ``\\x1b``-style escape. The CLI
  reaches it through :mod:`fnd.cli_output`.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Literal

__all__ = [
    "display_block",
    "display_line",
    "escape_surrogates",
    "fit",
    "terminal_block",
    "terminal_line",
]

# Whitespace that isn't a plain space maps to one space, one for one, so an
# intentional run (a label's ``loc  snippet`` gap) survives.
_NON_SPACE_WHITESPACE = re.compile(r"[^\S ]")

# Exactly the boundaries ``str.splitlines`` honours, so a block's lines agree
# with every consumer that splits it.
_LINE_BREAK = re.compile(r"\r\n|[\n\r\v\f\x1c-\x1e\x85\u2028\u2029]")

_STRIP_CATEGORIES = frozenset({"Cc", "Cf"})

# A lone surrogate is how Python holds a file-name byte that is not UTF-8. It
# cannot be encoded, so every sink spells it as the same visible escape.
_SURROGATE = re.compile(r"[\ud800-\udfff]")

# What acts on a terminal or hides text: C0/C1 controls except tab, line breaks,
# the bidi controls (CVE-2021-42574) and the invisible tag block.
_LINE_HAZARD = re.compile(
    r"[\x00-\x08\x0a-\x1f\x7f-\x9f\u2028\u2029\u061c\u200e\u200f\u202a-\u202e"
    r"\u2066-\u2069\ud800-\udfff\U000e0000-\U000e007f]"
)


def display_line(text: str) -> str:
    """``text`` as one line of printable characters and plain spaces, each
    occupying the cell width it is measured at."""
    text = _NON_SPACE_WHITESPACE.sub(" ", text)
    # Once separators are spaces, only Cc/Cf can make a string unprintable.
    if text.isprintable():
        return text
    text = escape_surrogates(text)
    return "".join(ch for ch in text if unicodedata.category(ch) not in _STRIP_CATEGORIES)


def display_block(text: str) -> str:
    """``text`` with each line break as one ``\\n`` and each line a
    :func:`display_line`."""
    return "\n".join(display_line(line) for line in _LINE_BREAK.split(text))


def _escape(match: re.Match[str]) -> str:
    return ascii(match.group())[1:-1]


def escape_surrogates(text: str) -> str:
    """``text`` with each lone surrogate as its ``\\udcff`` escape, the one
    spelling the TUI, the CLI and stored files share for such a byte."""
    return _SURROGATE.sub(_escape, text)


def terminal_line(text: str) -> str:
    """``text`` verbatim on one line, except control, line-break and hidden
    format characters, which show as visible escapes."""
    return _LINE_HAZARD.sub(_escape, text)


def terminal_block(text: str) -> str:
    """:func:`terminal_line` per line, each line break kept as one ``\\n``."""
    return "\n".join(terminal_line(line) for line in _LINE_BREAK.split(text))


def fit(text: str, cells: int, *, keep: Literal["start", "end"] = "start") -> str:
    """``text`` in at most ``cells`` terminal cells, an ellipsis marking the end
    dropped: measured in painted cells, so a wide or combining character never
    overflows a column a character count says it fits."""
    from rich.cells import split_graphemes

    spans, total = split_graphemes(text)
    if total <= cells:
        return text
    if cells <= 0:
        return ""
    room = cells - 1
    taken: list[str] = []
    used = 0
    # By grapheme: an emoji with its variation selector is one two-cell unit.
    for start, end, width in spans if keep == "start" else reversed(spans):
        if used + width > room:
            break
        taken.append(text[start:end])
        used += width
    kept = "".join(taken) if keep == "start" else "".join(reversed(taken))
    return f"{kept}\u2026" if keep == "start" else f"\u2026{kept}"
