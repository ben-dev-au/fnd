"""Text repairers (Strategy) injected into the recovery tiers.

Separate from the assessors in ``evaluators.py``: a repairer rewrites
Markdown, an evaluator only scores it.
"""

from __future__ import annotations

import re

_REPLACEMENT = "�"

# The letter runs a ligature glyph can stand for. The repairer recovers
# the exact dropped letters from the flat layer (so it isn't tied to this
# corpus's ff/fi/fl), but the recovered run must be an actual ligature —
# otherwise a skeleton like "_ows" would match "allows" (gap "all") as
# readily as "flows" (gap "fl"). This is the full Unicode ligature set
# (FB00–FB06) plus the extended f-ligatures real text fonts ship; extend it
# if a font surfaces another. Compared case-insensitively.
_LIGATURES = frozenset(
    {
        "ff",
        "fi",
        "fl",
        "ffi",
        "ffl",  # Unicode FB00–FB04 (the common case)
        "st",
        "ct",  # FB05/FB06 (long-s t, st) and old-style ct
        "ft",
        "fj",
        "fb",
        "fh",
        "fk",
        "ffj",
        "ffb",
        "ffh",
        "ffk",  # extended f-ligatures
    }
)
# Gap length bounds, derived from the ligature set (2 = ff … 3 = ffi): a
# cheap pre-filter so the skeleton regex stays selective before the
# membership check.
_MIN_GAP = min(len(lig) for lig in _LIGATURES)
_MAX_GAP = max(len(lig) for lig in _LIGATURES)
_GAP = rf"(\w{{{_MIN_GAP},{_MAX_GAP}}})"

# A word run carrying at least one U+FFFD AND at least one real word char,
# so a bare standalone U+FFFD (a non-ligature unmapped glyph) is never a
# match — repair only ever fires inside a word.
_BROKEN_WORD_RE = re.compile(
    rf"\w+{_REPLACEMENT}[\w{_REPLACEMENT}]*|{_REPLACEMENT}[\w{_REPLACEMENT}]*\w+"
)
_WORD_RE = re.compile(r"\w+")

# Cap U+FFFD per word; more than this is a row of unmapped glyphs, not a
# ligatured word, and is left untouched.
_MAX_GAPS = 4


class LigatureRepairer:
    """Rebuild words the layout text engine broke into U+FFFD, using the
    flat text layer as ground truth.

    Each U+FFFD stands for a ligature glyph the font couldn't map. The flat
    layer resolves those glyphs, so the repairer treats the broken token as
    a skeleton — literal letters around one or more gaps — and looks for the
    single flat-layer word that fits, where each gap is a real ligature (see
    ``_LIGATURES``). It recovers the exact dropped letters rather than
    brute-forcing a fixed list, so it handles any ligature in that set while
    ignoring non-ligature skeleton twins ("allows" vs "flows"). Casing
    follows the token: literal letters keep their case, recovered letters
    take the ground-truth word's case, and an all-caps token stays all-caps.

    Repair never guesses. A bare U+FFFD, a token with no fitting flat word,
    or an ambiguous skeleton (more than one distinct flat word fits) is left
    untouched — so a hard case is dropped, never corrupted. The true word is
    always present in the flat layer (same page), so a unique fit is always
    the right word.
    """

    def repair(self, markdown: str, flat: str) -> str:
        if _REPLACEMENT not in markdown:
            return markdown
        # Sorted so the match (and the casing it contributes) is
        # deterministic; uppercase variants sort first, so a leading-gap
        # capital is recovered in preference to a lowercase occurrence.
        vocab = sorted(set(_WORD_RE.findall(flat)))
        return _BROKEN_WORD_RE.sub(lambda m: self._fix(m.group(0), vocab), markdown)

    def _fix(self, word: str, vocab: list[str]) -> str:
        gaps = word.count(_REPLACEMENT)
        if not 1 <= gaps <= _MAX_GAPS or _REPLACEMENT * 2 in word:
            # Adjacent U+FFFD are separate dropped glyphs, not one ligature.
            return word
        skeleton = "".join(_GAP if ch == _REPLACEMENT else re.escape(ch) for ch in word)
        matcher = re.compile(f"(?i)^{skeleton}$")
        match: re.Match[str] | None = None
        seen: set[str] = set()
        for candidate in vocab:
            m = matcher.match(candidate)
            if m is None:
                continue
            if not all(gap.lower() in _LIGATURES for gap in m.groups()):
                continue  # a skeleton twin whose gap isn't a ligature (allows vs flows)
            seen.add(candidate.lower())
            if len(seen) > 1:
                return word  # two genuine ligature fits — refuse to guess
            if match is None:
                match = m
        if match is None:
            return word
        recovered = iter(match.groups())
        rebuilt = "".join(next(recovered) if ch == _REPLACEMENT else ch for ch in word)
        return rebuilt.upper() if word.isupper() else rebuilt
