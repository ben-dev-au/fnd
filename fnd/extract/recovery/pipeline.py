"""Chain-of-responsibility pipeline composing the extraction tiers.

The production layout path is the baseline; each later tier inspects the
current extraction and either refines it (scanned pages) or passes it
through (born-digital). Adding or deferring a tier is a one-line change
to the composition, keeping ``pdf.py`` free of nested gating ``if``s.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from fnd.extract.recovery.models import ExtractionContext, PageExtraction


class ExtractionTier(Protocol):
    """A single quality-gated stage. Decides whether it applies and
    returns an improved extraction, or returns ``current`` unchanged."""

    def refine(self, ctx: ExtractionContext, current: PageExtraction) -> PageExtraction: ...


class PageRecoveryPipeline:
    """Folds the page through each tier in order."""

    def __init__(self, tiers: Sequence[ExtractionTier]) -> None:
        self._tiers: tuple[ExtractionTier, ...] = tuple(tiers)

    def recover(self, ctx: ExtractionContext) -> PageExtraction:
        result = PageExtraction(markdown="")
        for tier in self._tiers:
            result = tier.refine(ctx, result)
        return result
