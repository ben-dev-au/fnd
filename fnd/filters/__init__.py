"""Unified filtering: one vocabulary, one model, one compiled gate."""

from fnd.filters.dimensions import DIMENSIONS, Dimension, note_kinds, rule_from_text
from fnd.filters.model import FileGate, FilterSpec, Rule, Unknown
from fnd.filters.text import build_gate

__all__ = [
    "DIMENSIONS",
    "Dimension",
    "FileGate",
    "FilterSpec",
    "Rule",
    "Unknown",
    "build_gate",
    "note_kinds",
    "rule_from_text",
]
