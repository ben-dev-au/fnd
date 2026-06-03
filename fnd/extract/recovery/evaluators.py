"""Quality assessors (Strategy) injected into the recovery tiers.

Kept separate from the tiers so thresholds live as tier config and the
scoring is unit-testable in isolation.
"""

from __future__ import annotations

import re

_ALPHA_TOKEN_RE = re.compile(r"[A-Za-z]{3,}")


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
