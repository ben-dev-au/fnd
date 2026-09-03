"""The two projections of a :class:`FilterSpec`: DSL text, and a compiled gate.

Rendering is canonical, so ``parse(render(spec)) == spec`` for anything the
pickers can express. Every clause is parenthesised when more than one is
joined — without that, a raw clause containing ``OR`` captures its neighbours
and a source restricted to PDFs silently admits everything else.
"""

from __future__ import annotations

from fnd.filter_dsl import And, FilterError
from fnd.filter_dsl import parse as parse_dsl
from fnd.filters._unparse import unparse
from fnd.filters.dimensions import DIMENSIONS, recognise, rule_from_text
from fnd.filters.model import FileGate, FilterSpec, Rule

__all__ = ["build_gate", "gate_from_text", "parse", "render"]

# Dimensions whose value is free text, not a picker value; they render as
# themselves and are never recognised back out of an AST.
_EXPRESSION_IDS = ("frontmatter", "expression")


def _clauses(spec: FilterSpec) -> list[str]:
    out: list[str] = []
    for dim in DIMENSIONS:
        if dim.id in _EXPRESSION_IDS or dim.id.startswith("exclude_tags_"):
            continue
        value = getattr(spec, dim.id, None)
        if value in (None, (), "", []):
            continue
        out.append(dim.render(value))
    if spec.expression:
        out.append(spec.expression)
    out.extend(spec.raw)
    return out


def render(spec: FilterSpec) -> str:
    """The spec as one DSL expression. ``frontmatter`` is excluded: it carries
    a kind scope the flat text form cannot express."""
    clauses = _clauses(spec)
    if not clauses:
        return ""
    if len(clauses) == 1:
        return clauses[0]
    return " AND ".join(f"({c})" for c in clauses)


def _split_and(node: object) -> list[object]:
    """Flatten a left-nested AND chain. An OR or NOT at the root is one clause."""
    if isinstance(node, And):
        return _split_and(node.left) + _split_and(node.right)
    return [node]


def parse(text: str, *, base: FilterSpec | None = None) -> FilterSpec:
    """Text back into a spec. Unrecognised clauses land in ``raw`` verbatim.

    Raises :class:`FilterError` if the text does not parse at all.
    """
    spec = base or FilterSpec()
    stripped = text.strip()
    if not stripped:
        return spec
    node = parse_dsl(stripped)
    updates: dict[str, object] = {}
    tags: list[str] = []
    raw: list[str] = []
    for clause in _split_and(node):
        found = recognise(clause)
        if found is None:
            raw.append(_render_node(clause, stripped))
            continue
        dim_id, value = found
        if dim_id.startswith("exclude_tags"):
            tags.extend(str(v) for v in (value if isinstance(value, list) else [value]))
            continue
        updates[dim_id] = tuple(value) if isinstance(value, list) else value
    if tags:
        updates["exclude_tags"] = tuple(dict.fromkeys(tags))
    if raw:
        updates["raw"] = tuple(raw)
    from dataclasses import replace

    return replace(spec, **updates)  # type: ignore[arg-type]


def _render_node(node: object, original: str) -> str:
    """A clause we could not recognise, kept as text.

    The AST discards the user's brackets, so an unrecognised clause from a
    multi-clause expression is re-rendered from its parts rather than sliced
    out of the original.
    """
    return unparse(node) or original


def build_gate(spec: FilterSpec) -> FileGate:
    """Compile every populated dimension into one gate."""
    rules: list[Rule] = []
    for dim in DIMENSIONS:
        if dim.id.startswith("exclude_tags_"):
            continue
        value = getattr(spec, dim.id, None)
        if value in (None, (), "", []):
            continue
        try:
            rule = dim.rule(value)
        except FilterError:
            raise
        if rule is not None:
            rules.append(rule)
    for clause in spec.raw:
        rules.append(rule_from_text(clause))
    return FileGate.of(rules)


def gate_from_text(text: str) -> FileGate:
    """Convenience for callers holding text rather than a spec."""
    return build_gate(parse(text))
