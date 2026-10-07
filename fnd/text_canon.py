"""The one Unicode policy for text entering fnd.

Extracted document text, file names, queries, tags and config values all pass
through :func:`canonical`, so the index and every query agree on what a
character is: a decomposed ``é`` (macOS file names) and a composed one are the
same letter, a ``ﬁ`` ligature from a PDF is ``fi``, and an invisible character
can neither split a word nor hide inside one. Equality of names and tags goes
through :func:`identity_key`.
"""

from __future__ import annotations

import re
import unicodedata

__all__ = ["canonical", "identity_key"]

_LINE_BREAK = re.compile(r"\r\n|[\r\v\f\x1c-\x1e\x85\u2028\u2029]")

# Presentation forms with a plain spelling: Latin ligatures and full-width
# ASCII letters and digits. Full-width punctuation stays, it is CJK typography.
_PRESENTATION = re.compile(r"[\ufb00-\ufb06\uff10-\uff19\uff21-\uff3a\uff41-\uff5a]")

# Format characters that shape a script (ZWNJ, ZWJ: Persian, Indic, emoji)
# are part of the text; every other Cc/Cf is not.
_KEPT_FORMAT = frozenset("\u200c\u200d")


def _visible(ch: str) -> bool:
    if ch in "\t\n" or ch in _KEPT_FORMAT:
        return True
    return unicodedata.category(ch) not in ("Cc", "Cf")


def canonical(text: str) -> str:
    """``text`` in NFC, with presentation forms folded, every line break as
    ``\\n``, and no control or invisible format character but tab."""
    text = _LINE_BREAK.sub("\n", text)
    if not text.isprintable():
        text = "".join(ch for ch in text if _visible(ch))
    text = _PRESENTATION.sub(lambda m: unicodedata.normalize("NFKC", m.group()), text)
    return unicodedata.normalize("NFC", text)


def identity_key(text: str) -> str:
    """The key two names or tags are the same under: :func:`canonical`, case
    folded, whitespace runs collapsed and trimmed."""
    return " ".join(canonical(text).casefold().split())
