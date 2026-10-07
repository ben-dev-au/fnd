"""The one Unicode policy for text entering fnd.

Extracted document text, file names, queries, tags and config values all pass
through :func:`canonical`, so the index and every query agree on what a
character is: a decomposed ``é`` (macOS file names) and a composed one are the
same letter, a ``ﬁ`` ligature from a PDF is ``fi``, and an invisible character
can neither split a word nor hide inside one. File paths are matched in this
form (globs, ignore files, filter rules) but stored and opened as their bytes.
"""

from __future__ import annotations

import re
import unicodedata

__all__ = ["canonical", "decode"]

# The line model of an editor and of markdown-it: only CR and CRLF end a line,
# so a deep-link line number and a preview section match the file as opened.
_LINE_BREAK = re.compile(r"\r\n?")
_SEPARATOR = re.compile(r"[\v\f\x1c-\x1e\x85\u2028\u2029]")

# Presentation forms with a plain spelling: Latin ligatures and full-width
# ASCII letters and digits. Full-width punctuation stays, it is CJK typography.
_PRESENTATION = re.compile(r"[\ufb00-\ufb06\uff10-\uff19\uff21-\uff3a\uff41-\uff5a]")

# Every Cc and Cf character (Unicode 15.1) but tab, newline, and the ZWNJ and ZWJ
# that shape a script. One class, not a per-character category lookup: measured
# 16 ms against 131 ms on 1.1 MB. A test holds it to unicodedata.
_HIDDEN = re.compile(
    "[\x00-\x08\x0b-\x1f\x7f-\x9f\xad\u0600-\u0605\u061c\u06dd\u070f\u0890\u0891"
    "\u08e2\u180e\u200b\u200e\u200f\u202a-\u202e\u2060-\u2064\u2066-\u206f\ufeff"
    "\ufff9-\ufffb\U000110bd\U000110cd\U00013430-\U0001343f\U0001bca0-\U0001bca3"
    "\U0001d173-\U0001d17a\U000e0001\U000e0020-\U000e007f]"
)


def canonical(text: str) -> str:
    """``text`` in NFC, with presentation forms folded, CR and CRLF as ``\\n``,
    other separators as a space, and no control or invisible format character but tab."""
    text = _HIDDEN.sub("", _SEPARATOR.sub(" ", _LINE_BREAK.sub("\n", text)))
    text = _PRESENTATION.sub(lambda m: unicodedata.normalize("NFKC", m.group()), text)
    return unicodedata.normalize("NFC", text)


_BOMS = (
    (b"\xff\xfe\x00\x00", "utf-32"),
    (b"\x00\x00\xfe\xff", "utf-32"),
    (b"\xff\xfe", "utf-16"),
    (b"\xfe\xff", "utf-16"),
)


def decode(raw: bytes) -> str:
    """File bytes as :func:`canonical` text: UTF-16 or UTF-32 by their BOM, else
    UTF-8 (a BOM dropped, a stray bad byte as U+FFFD); a file with more bad
    sequences than good multibyte ones is legacy Windows-1252."""
    for bom, codec in _BOMS:
        if raw.startswith(bom):
            return canonical(raw.decode(codec, errors="replace"))
    try:
        return canonical(raw.decode("utf-8-sig"))
    except UnicodeDecodeError:
        text = raw.decode("utf-8-sig", errors="replace")
    bad = text.count("\ufffd")
    if bad >= sum(1 for ch in text if ord(ch) > 0x7F) - bad:
        text = raw.decode("cp1252", errors="replace")
    return canonical(text)
