"""Compile a :class:`FilterSpec` into the gate the walker evaluates."""

from __future__ import annotations

from fnd.filters.dimensions import DIMENSIONS, rule_from_text
from fnd.filters.model import FileGate, FilterSpec, Rule

__all__ = ["build_gate"]


def build_gate(spec: FilterSpec) -> FileGate:
    """Every populated dimension as one gate.

    ``frontmatter`` is not compiled here: it carries a kind scope, so the
    walker builds it with :func:`rule_from_text` and the note-kind set.
    """
    rules: list[Rule] = []
    for dim in DIMENSIONS:
        if dim.id == "frontmatter":
            continue
        value = getattr(spec, dim.id, None)
        if value in (None, (), "", [], {}):
            continue
        rule = dim.rule(value)
        if rule is not None:
            rules.append(rule)
    rules.extend(rule_from_text(clause) for clause in spec.raw)
    return FileGate.of(rules)
