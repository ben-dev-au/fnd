"""The text view of a :class:`FilterSpec`, and the way back.

The rows and the expression are two views of one filter set: editing either
must show up in the other. Rendering is canonical; parsing recognises the
clause shapes the rows can edit and keeps everything else verbatim.

A clause the user typed as free text that happens to match a row's shape
becomes that row. That is the point — it is how typing
``file.kind in ['pdf']`` makes the File type row show PDF — so the round-trip
guarantee is that the *filter behaves the same*, not that a value stays in the
field it started in.
"""

from __future__ import annotations

import datetime as dt
import re

from fnd.filter_dsl import And, Compare, FieldIn, FilterError, In, Not, Or
from fnd.filter_dsl import parse as parse_dsl
from fnd.filters.dimensions import NOTE_KINDS
from fnd.filters.model import FilterSpec

__all__ = ["parse", "render"]

_BARE = re.compile(r"[A-Za-z_][A-Za-z0-9_\-.]*\Z")
_TAG_FACT = "file.tags.os"
_COMPARISONS: dict[tuple[str, str], str] = {
    ("file.size", ">="): "min_size",
    ("file.size", "<="): "max_size",
    ("file.created", ">="): "created_after",
    ("file.created", "<="): "created_before",
    ("file.modified", ">="): "modified_after",
    ("file.modified", "<="): "modified_before",
}
_BY_FIELD = {v: k for k, v in _COMPARISONS.items()}


def _field(name: str) -> str:
    return name if _BARE.match(name) else f'"{name}"'


def _value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, str):
        # The grammar has no escape for a quote inside a string literal, so a
        # value carrying one cannot be written as text. Callers compile such
        # values directly (fnd.filters.dimensions) and never route them here.
        return "'" + value.replace("'", "") + "'"
    return str(value)


def _unparse(node: object, *, depth: int = 0) -> str:
    if isinstance(node, Compare):
        return f"{_field(node.field)} {node.op} {_value(node.value)}"
    if isinstance(node, In):
        op = "not in" if node.negated else "in"
        return f"{_value(node.value)} {op} {_field(node.field)}"
    if isinstance(node, FieldIn):
        op = "not in" if node.negated else "in"
        return f"{_field(node.field)} {op} [{', '.join(_value(v) for v in node.values)}]"
    if isinstance(node, Not):
        return f"NOT ({_unparse(node.operand, depth=depth + 1)})"
    if isinstance(node, (And, Or)):
        keyword = "AND" if isinstance(node, And) else "OR"
        text = f"{_unparse(node.left, depth=depth + 1)} {keyword} {_unparse(node.right, depth=depth + 1)}"
        return f"({text})" if depth else text
    return ""


def _note_scope() -> str:
    kinds = ", ".join(_value(k) for k in sorted(NOTE_KINDS))
    return f"file.kind in [{kinds}]"


def render(spec: FilterSpec) -> str:
    """The whole filter set as one expression.

    Every clause is parenthesised when more than one is joined: a raw clause
    containing ``OR`` would otherwise capture its neighbours and widen the
    filter it was meant to narrow.
    """
    clauses: list[str] = []
    if spec.kinds:
        clauses.append(f"file.kind in [{', '.join(_value(k) for k in spec.kinds)}]")
    for tag in spec.exclude_tags:
        clauses.append(f"NOT ({_value(tag)} in {_TAG_FACT})")
    for field_name, (fact, op) in _BY_FIELD.items():
        value = getattr(spec, field_name, None)
        if value is not None:
            clauses.append(f"{fact} {op} {_value(value)}")
    if spec.frontmatter:
        # The kind scope the compiler applies is written out, so the text says
        # what the rule actually does rather than silently dropping it.
        clauses.append(f"NOT ({_note_scope()}) OR ({spec.frontmatter})")
    if spec.expression:
        clauses.append(spec.expression)
    clauses.extend(spec.raw)
    if not clauses:
        return ""
    if len(clauses) == 1:
        return clauses[0]
    return " AND ".join(f"({c})" for c in clauses)


def _split_and(node: object) -> list[object]:
    if isinstance(node, And):
        return _split_and(node.left) + _split_and(node.right)
    return [node]


def _match_frontmatter(node: object) -> str | None:
    """``NOT (file.kind in [notes]) OR (expr)`` — the note-scoped form."""
    if not isinstance(node, Or):
        return None
    left = node.left
    if not (isinstance(left, Not) and isinstance(left.operand, FieldIn)):
        return None
    inner = left.operand
    if inner.field != "file.kind" or inner.negated:
        return None
    if {str(v) for v in inner.values} != set(NOTE_KINDS):
        return None
    return _unparse(node.right)


def _recognise(node: object) -> tuple[str, object] | None:
    if isinstance(node, FieldIn) and node.field == "file.kind" and not node.negated:
        return "kinds", tuple(str(v) for v in node.values)
    if isinstance(node, Not) and isinstance(node.operand, In):
        inner = node.operand
        if inner.field == _TAG_FACT and not inner.negated:
            return "exclude_tags", str(inner.value)
    if isinstance(node, Compare):
        field_name = _COMPARISONS.get((node.field, node.op))
        if field_name is not None:
            return field_name, node.value
    scoped = _match_frontmatter(node)
    if scoped is not None:
        return "frontmatter", scoped
    return None


def parse(text: str) -> FilterSpec:
    """Text back into a spec. Raises :class:`FilterError` if it does not parse.

    Clauses the rows cannot express land in ``expression`` (the first) and
    ``raw`` (any further ones), so nothing the user wrote is dropped.
    """
    from dataclasses import replace

    stripped = text.strip()
    if not stripped:
        return FilterSpec()
    updates: dict[str, object] = {}
    tags: list[str] = []
    leftover: list[str] = []
    for clause in _split_and(parse_dsl(stripped)):
        found = _recognise(clause)
        if found is None:
            leftover.append(_unparse(clause) or stripped)
            continue
        name, value = found
        if name == "exclude_tags":
            tags.append(str(value))
        else:
            updates[name] = value
    if tags:
        updates["exclude_tags"] = tuple(dict.fromkeys(tags))
    if leftover:
        updates["expression"] = leftover[0]
        if len(leftover) > 1:
            updates["raw"] = tuple(leftover[1:])
    return replace(FilterSpec(), **updates)  # type: ignore[arg-type]


def parse_or_error(text: str) -> tuple[FilterSpec | None, FilterError | None]:
    """Non-raising variant, for a live-validating editor."""
    try:
        return parse(text), None
    except FilterError as e:
        return None, e
