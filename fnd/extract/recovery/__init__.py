"""Tiered page-recovery pipeline for structured PDF extraction.

A Chain of Responsibility (the pipeline) over Strategy stages (the
tiers): the production layout path is the baseline; later tiers recover
invisible-text scanned pages that the layout parser drops. See
``pipeline.py`` for composition.
"""

from __future__ import annotations

from fnd.extract.recovery.evaluators import (
    CoverageEvaluator,
    LegibilityEvaluator,
    alpha_tokens,
)
from fnd.extract.recovery.models import (
    FLAG_DOCLING_INVOKED,
    FLAG_FIGURE_INCOMPLETE,
    FLAG_LIGATURE_REPAIRED,
    FLAG_LOW_QUALITY,
    FLAG_TEXTURE_RECOVERED,
    ExtractionContext,
    PageExtraction,
)
from fnd.extract.recovery.pipeline import ExtractionTier, PageRecoveryPipeline
from fnd.extract.recovery.repairers import LigatureRepairer
from fnd.extract.recovery.tiers import (
    TABLE_CAPTION_RE,
    DoclingTableTier,
    FlatFallbackTier,
    InvisibleTextTier,
    LegibilityReprocessTier,
    LigatureRepairTier,
    ProductionLayoutTier,
)

__all__ = [
    "FLAG_DOCLING_INVOKED",
    "FLAG_FIGURE_INCOMPLETE",
    "FLAG_LIGATURE_REPAIRED",
    "FLAG_LOW_QUALITY",
    "FLAG_TEXTURE_RECOVERED",
    "TABLE_CAPTION_RE",
    "CoverageEvaluator",
    "DoclingTableTier",
    "ExtractionContext",
    "ExtractionTier",
    "FlatFallbackTier",
    "InvisibleTextTier",
    "LegibilityEvaluator",
    "LegibilityReprocessTier",
    "LigatureRepairTier",
    "LigatureRepairer",
    "PageExtraction",
    "PageRecoveryPipeline",
    "ProductionLayoutTier",
    "alpha_tokens",
]
