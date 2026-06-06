"""Text repairers (Strategy) injected into the recovery tiers.

Separate from the assessors in ``evaluators.py``: a repairer rewrites
Markdown, an evaluator only scores it.
"""

from __future__ import annotations

import re
from itertools import product

_REPLACEMENT = "�"

# f-ligatures the layout text engine drops to U+FFFD when a font lacks
# ToUnicode entries for them. Longest first so a vocab tie resolves to the
# wider ligature ("ffi" over "ff"/"fi").
_LIGATURES = ("ffi", "ffl", "ff", "fi", "fl")

# A word run carrying at least one U+FFFD AND at least one real word char,
# so a bare standalone U+FFFD (a non-ligature unmapped glyph) is never a
# match — repairing only ever fires inside a word.
_BROKEN_WORD_RE = re.compile(
    rf"\w+{_REPLACEMENT}[\w{_REPLACEMENT}]*|{_REPLACEMENT}[\w{_REPLACEMENT}]*\w+"
)
_WORD_RE = re.compile(r"\w+")

# Most U+FFFD-per-word counts are 1, occasionally 2. Cap the brute-forced
# expansion at 5**4 candidates; tokens with more slots are pathological
# (not real ligatures — e.g. a row of unmapped dingbats) and left as is.
_MAX_SLOTS = 4


class LigatureRepairer:
    """Rebuild words the layout text engine broke into U+FFFD, using the
    flat text layer as ground truth.

    The flat layer (``page.get_text("text")``) resolves the ligature
    glyphs the layout engine cannot, so its vocabulary disambiguates which
    expansion a given U+FFFD stood for. Only words whose ligature-expanded
    form appears in that vocabulary are rewritten; standalone U+FFFD (a
    non-ligature unmapped glyph) and unmatched words are left untouched.
    """

    def repair(self, markdown: str, flat: str) -> str:
        if _REPLACEMENT not in markdown:
            return markdown
        vocab = {w.lower() for w in _WORD_RE.findall(flat)}
        return _BROKEN_WORD_RE.sub(lambda m: self._fix(m.group(0), vocab), markdown)

    def _fix(self, word: str, vocab: set[str]) -> str:
        slots = word.count(_REPLACEMENT)
        if not 1 <= slots <= _MAX_SLOTS:
            return word
        for combo in product(_LIGATURES, repeat=slots):
            candidate = self._expand(word, combo)
            if candidate.lower() in vocab:
                return candidate
        return word

    @staticmethod
    def _expand(word: str, combo: tuple[str, ...]) -> str:
        out: list[str] = []
        i = 0
        for char in word:
            if char == _REPLACEMENT:
                out.append(combo[i])
                i += 1
            else:
                out.append(char)
        return "".join(out)
