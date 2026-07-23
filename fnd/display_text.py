"""Normalise arbitrary text into a form safe for single-line, fixed-width
display (results-row labels, CLI snippets).

Extracted PDF / office body text carries layout artefacts — tabs, hard line
breaks, zero-width joiners, bidi overrides — whose *rendered* width disagrees
with the cell width Rich measures: a raw ``\\t`` measures zero cells yet a
terminal expands it to the next tab stop. In a bordered, fixed-width pane that
mismatch over-runs the content region and corrupts the border. This module is
the single place that guarantee lives, shared by the snippet builder
(:mod:`fnd.query`) and the results-row label builder (:mod:`fnd.tui`) so both
layers agree without either importing the other.
"""

from __future__ import annotations

import re
import unicodedata

__all__ = ["sanitise_display_text"]

# Whitespace that isn't a plain space — tab / newline / CR / form-feed /
# vertical-tab plus the Unicode separators (incl. the 2-cell ideographic space
# U+3000) — maps to a single plain space, one for one, so intentional space
# runs (a label's ``loc  snippet`` gap) survive untouched.
_NON_SPACE_WHITESPACE = re.compile(r"[^\S ]")

# Categories dropped outright once whitespace is mapped: C0/C1 controls (Cc)
# and format characters (Cf) — zero-width space/joiner, soft hyphen, BOM and
# the bidi overrides — none of which belong in a display cell.
_STRIP_CATEGORIES = frozenset({"Cc", "Cf"})


def sanitise_display_text(text: str) -> str:
    """Return ``text`` safe for single-line, fixed-width display: no line
    breaks, control, zero-width, or bidi characters, so every kept character
    occupies exactly the cell width it is measured at."""
    text = _NON_SPACE_WHITESPACE.sub(" ", text)
    # Fast path: once separators are spaces, a printable string has no Cc/Cf
    # left to strip (``str.isprintable`` is false for exactly those, and any
    # surviving wide/private-use glyph is border-safe and kept).
    if text.isprintable():
        return text
    return "".join(ch for ch in text if unicodedata.category(ch) not in _STRIP_CATEGORIES)
