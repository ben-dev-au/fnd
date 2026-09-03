"""AST back to DSL text.

The parser discards the user's brackets, so a clause lifted out of a larger
expression cannot be sliced from the original source — it is re-rendered here,
in canonical form, with precedence-correct parentheses.
"""

from __future__ import annotations

import datetime as dt
import re

from fnd.filter_dsl import And, Compare, FieldIn, In, Not, Or

__all__ = ["unparse"]

_BARE = re.compile(r"[A-Za-z_][A-Za-z0-9_\-.]*\Z")


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
        # value containing one cannot be rendered back; drop it rather than
        # emit text that will not re-parse.
        return "'" + value.replace("'", "") + "'"
    return str(value)


def unparse(node: object, *, _depth: int = 0) -> str:
    if isinstance(node, Compare):
        return f"{_field(node.field)} {node.op} {_value(node.value)}"
    if isinstance(node, In):
        op = "not in" if node.negated else "in"
        return f"{_value(node.value)} {op} {_field(node.field)}"
    if isinstance(node, FieldIn):
        op = "not in" if node.negated else "in"
        joined = ", ".join(_value(v) for v in node.values)
        return f"{_field(node.field)} {op} [{joined}]"
    if isinstance(node, Not):
        return f"NOT ({unparse(node.operand, _depth=_depth + 1)})"
    if isinstance(node, (And, Or)):
        keyword = "AND" if isinstance(node, And) else "OR"
        left = unparse(node.left, _depth=_depth + 1)
        right = unparse(node.right, _depth=_depth + 1)
        text = f"{left} {keyword} {right}"
        return f"({text})" if _depth else text
    return ""
