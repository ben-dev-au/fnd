"""Tiered page-recovery pipeline for structured PDF extraction.

A Chain of Responsibility (the pipeline) over Strategy stages (the
tiers): the production layout path is the baseline; later tiers recover
invisible-text scanned pages that the layout parser drops. See
``pipeline.py`` for composition.
"""

from __future__ import annotations

from fnd.extract.recovery.evaluators import CoverageEvaluator, alpha_tokens
from fnd.extract.recovery.models import (
    FLAG_DOCLING_INVOKED,
    FLAG_FIGURE_INCOMPLETE,
    FLAG_LOW_QUALITY,
    FLAG_TEXTURE_RECOVERED,
    ExtractionContext,
    PageExtraction,
)
from fnd.extract.recovery.pipeline import ExtractionTier, PageRecoveryPipeline
from fnd.extract.recovery.tiers import (
    TABLE_CAPTION_RE,
    DoclingTableTier,
    InvisibleTextTier,
    ProductionLayoutTier,
)

__all__ = [
    "FLAG_DOCLING_INVOKED",
    "FLAG_FIGURE_INCOMPLETE",
    "FLAG_LOW_QUALITY",
    "FLAG_TEXTURE_RECOVERED",
    "TABLE_CAPTION_RE",
    "CoverageEvaluator",
    "DoclingTableTier",
    "ExtractionContext",
    "ExtractionTier",
    "InvisibleTextTier",
    "PageExtraction",
    "PageRecoveryPipeline",
    "ProductionLayoutTier",
    "alpha_tokens",
]
