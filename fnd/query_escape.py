"""User text in tantivy's query-string syntax: every character the parser
treats as an operator is escaped, so it reads the words that were typed.

Measured on tantivy 0.26: ``don\\'t`` and ``10\\:30`` parse to the analyser's own
phrase (``don t``, ``10 30``), where the raw forms fail as syntax. The compiler
in :mod:`fnd.query_compile` is the one place AST leaves reach the parser.
"""

from __future__ import annotations

__all__ = ["literal", "literal_phrase"]

_SYNTAX = frozenset("+-&|!(){}[]^\"'~*?:\\/<>=")


def literal(text: str) -> str:
    """``text`` with each query-syntax character escaped."""
    return "".join(f"\\{ch}" if ch in _SYNTAX else ch for ch in text)


def literal_phrase(text: str, *, slop: int = 0) -> str:
    """``text`` as one escaped phrase, with ``~slop`` when positive."""
    quoted = f'"{literal(text)}"'
    return f"{quoted}~{slop}" if slop else quoted
