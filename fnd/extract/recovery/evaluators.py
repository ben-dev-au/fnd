"""Quality assessors (Strategy) injected into the recovery tiers.

Kept separate from the tiers so thresholds live as tier config and the
scoring is unit-testable in isolation.
"""

from __future__ import annotations

import re
from pathlib import Path

_ALPHA_TOKEN_RE = re.compile(r"[A-Za-z]{3,}")

# System word list used to judge OCR legibility. Suffix-stemming catches
# inflected forms a bare dictionary misses (which would false-flag clean
# prose as garbled).
_SYSTEM_DICT = Path("/usr/share/dict/words")
_LEGR_SUFFIXES = ("s", "es", "ed", "ing", "ly", "er", "ers", "tion", "tions", "ment", "ments", "d", "y")
_PROSE_TOKEN_RE = re.compile(r"[A-Za-z]+")
_FENCE_RE = re.compile(r"```.*?```", re.S)
_CAMEL_RE = re.compile(r"[a-z][A-Z]")


def alpha_tokens(text: str) -> set[str]:
    """Distinct lowercased runs of >=3 ASCII letters. Mirrors the
    measured probe's tokenizer so the gate's thresholds carry over."""
    return set(_ALPHA_TOKEN_RE.findall(text.lower()))


class CoverageEvaluator:
    """Scores how much of the flat text layer a Markdown extraction
    captured — the signal that distinguishes a dropped invisible-text
    page (coverage ~0.08) from a faithfully extracted one (~0.95)."""

    def flat_token_count(self, flat: str) -> int:
        """Distinct flat-layer tokens — the gate's token floor input."""
        return len(alpha_tokens(flat))

    def coverage(self, markdown: str, flat: str) -> float:
        """Fraction of the flat layer's tokens present in ``markdown``."""
        flat_tokens = alpha_tokens(flat)
        if not flat_tokens:
            return 0.0
        return len(alpha_tokens(markdown) & flat_tokens) / len(flat_tokens)


def _is_camel(token: str) -> bool:
    """A code identifier: an internal lower→upper transition, or an
    initial capital followed by another capital (e.g. ``AbstractFactory``,
    ``HTTPServer``). These read as legible even though no dictionary lists
    them."""
    if _CAMEL_RE.search(token):
        return True
    return token[:1].isupper() and any(c.isupper() for c in token[1:])


class LegibilityEvaluator:
    """Scores how readable a recovered page's prose is — the signal that
    separates clean baked OCR from a garbled scan. Operates on prose only
    (code fences stripped, since identifiers aren't dictionary words).

    The dictionary loads lazily from the system word list; pass an
    explicit set to test deterministically or to run where it's absent.
    """

    def __init__(self, dictionary: set[str] | None = None) -> None:
        self._dictionary = dictionary
        self._loaded = dictionary is not None
        # True only when the lazy system-dict load was attempted and the
        # word list was absent (Windows / minimal containers). An explicit
        # empty dict is NOT "unavailable" — it deliberately isolates the
        # CamelCase rule, so we still score against it.
        self._dict_unavailable = False

    def _dict(self) -> set[str]:
        if not self._loaded:
            self._dictionary = _load_system_dict()
            self._loaded = True
            self._dict_unavailable = not self._dictionary
        return self._dictionary or set()

    def _known(self, token: str) -> bool:
        words = self._dict()
        tl = token.lower()
        if tl in words:
            return True
        return any(
            tl.endswith(suffix) and len(tl) - len(suffix) >= 3 and tl[: -len(suffix)] in words
            for suffix in _LEGR_SUFFIXES
        )

    def prose_legr(self, markdown: str) -> tuple[float | None, int]:
        """``(legible fraction, prose token count)``. The fraction is the
        share of prose tokens that are dictionary-known (suffix-stemmed)
        or CamelCase identifiers; ``None`` when there's no prose to judge."""
        prose = _FENCE_RE.sub(" ", markdown)
        tokens = [t for t in _PROSE_TOKEN_RE.findall(prose) if len(t) >= 2]
        if not tokens:
            return None, 0
        self._dict()  # trigger lazy load so _dict_unavailable is set
        if self._dict_unavailable:
            # No word list to judge against — abstain rather than scoring
            # every page 0.0 (which would make the tier reprocess all prose).
            return None, len(tokens)
        legible = sum(1 for t in tokens if self._known(t) or _is_camel(t))
        return legible / len(tokens), len(tokens)


def _load_system_dict() -> set[str]:
    try:
        with _SYSTEM_DICT.open(encoding="utf-8", errors="ignore") as fh:
            return {w.strip().lower() for w in fh if w.strip()}
    except OSError:
        return set()
