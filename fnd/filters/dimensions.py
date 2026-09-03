"""The shared filter vocabulary.

One :class:`Dimension` per thing a user can filter on. Each knows how to
render its value as DSL text, recognise that text again in an AST, and compile
it to a :class:`Rule`. Query time reuses the same names and value vocabularies
(``fnd.vocabulary``) so a config ``kinds = ["pdf"]`` and a ``--kind pdf`` flag
mean the same thing; it keeps its own tantivy compiler, because size, globs and
ignore files have no index field to compile against.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, Protocol

from fnd.filter_dsl import Compare, FieldIn, FilterError, In, Not, compile_filter, referenced_fields
from fnd.filter_dsl import parse as parse_dsl
from fnd.filters.model import Rule
from fnd.kinds import KINDS_IN_CATEGORY

__all__ = ["DIMENSIONS", "Dimension", "dimension", "rule_from_text"]

# Frontmatter is a note-format convention; the registry's own category is the
# source of truth so adding .mdx or .org needs no edit here.
NOTE_KINDS: Final[frozenset[str]] = frozenset(KINDS_IN_CATEGORY.get("notes", ()))


def _as_list(value: object) -> list[object]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [v for v in value if v]
    return [value] if value else []


def _quote(value: object) -> str:
    if isinstance(value, str):
        return "'" + value.replace("'", "") + "'"
    if isinstance(value, dt.date):
        return value.isoformat()
    return str(value)


class Dimension(Protocol):
    """One filterable attribute, projected to text and to a predicate."""

    @property
    def id(self) -> str: ...

    def render(self, value: object) -> str: ...

    def match(self, node: object) -> object | None: ...

    def rule(self, value: object) -> Rule | None: ...


@dataclass(frozen=True, slots=True)
class _ListDimension:
    """``file.kind in ['pdf','md']`` — a set membership over a scalar fact."""

    id: str
    fact: str

    def render(self, value: object) -> str:
        values = _as_list(value)
        return f"{self.fact} in [{', '.join(_quote(v) for v in values)}]"

    def match(self, node: object) -> object | None:
        if isinstance(node, FieldIn) and node.field == self.fact and not node.negated:
            return list(node.values)
        return None

    def rule(self, value: object) -> Rule | None:
        values = _as_list(value)
        if not values:
            return None
        return _compile(self.render(values))


@dataclass(frozen=True, slots=True)
class _TagExcludeDimension:
    """``NOT ('no_index' in file.tags.os)`` — one clause per excluded tag."""

    id: str
    fact: str

    def render(self, value: object) -> str:
        values = _as_list(value)
        return " AND ".join(f"NOT ({_quote(v)} in {self.fact})" for v in values)

    def match(self, node: object) -> object | None:
        inner = node.operand if isinstance(node, Not) else None
        if isinstance(inner, In) and inner.field == self.fact and not inner.negated:
            return [inner.value]
        return None

    def rule(self, value: object) -> Rule | None:
        values = _as_list(value)
        if not values:
            return None
        return _compile(self.render(values))


@dataclass(frozen=True, slots=True)
class _ComparisonDimension:
    """A single ordered comparison against a fact: size or a date bound."""

    id: str
    fact: str
    op: str

    def render(self, value: object) -> str:
        return f"{self.fact} {self.op} {_quote(value)}"

    def match(self, node: object) -> object | None:
        if isinstance(node, Compare) and node.field == self.fact and node.op == self.op:
            return node.value
        return None

    def rule(self, value: object) -> Rule | None:
        if value is None:
            return None
        return _compile(self.render(value))


@dataclass(frozen=True, slots=True)
class _ExpressionDimension:
    """A raw predicate the user wrote. ``applies_to`` scopes it to kinds that
    can actually supply what it asks about."""

    id: str
    applies_to: frozenset[str] | None

    def render(self, value: object) -> str:
        return str(value)

    def match(self, node: object) -> object | None:
        return None  # never recognised — it is the fallback

    def rule(self, value: object) -> Rule | None:
        text = str(value or "").strip()
        if not text:
            return None
        return _compile(text, applies_to=self.applies_to)


def _compile(text: str, *, applies_to: frozenset[str] | None = None) -> Rule:
    node = parse_dsl(text)
    return Rule(
        predicate=compile_filter(text),
        text=text,
        facts=referenced_fields(node),
        applies_to=applies_to,
    )


def rule_from_text(text: str, *, applies_to: frozenset[str] | None = None) -> Rule:
    """Compile arbitrary DSL text into a rule. Raises :class:`FilterError`."""
    return _compile(text, applies_to=applies_to)


DIMENSIONS: Final[tuple[Dimension, ...]] = (
    _ListDimension("kinds", "file.kind"),
    _TagExcludeDimension("exclude_tags_os", "file.tags.os"),
    _TagExcludeDimension("exclude_tags_frontmatter", "file.tags.frontmatter"),
    _TagExcludeDimension("exclude_tags", "file.tags.all"),
    _ComparisonDimension("min_size", "file.size", ">="),
    _ComparisonDimension("max_size", "file.size", "<="),
    _ComparisonDimension("created_after", "file.created", ">="),
    _ComparisonDimension("created_before", "file.created", "<="),
    _ComparisonDimension("modified_after", "file.modified", ">="),
    _ComparisonDimension("modified_before", "file.modified", "<="),
    _ExpressionDimension("frontmatter", NOTE_KINDS),
    _ExpressionDimension("expression", None),
)

_BY_ID: Final[dict[str, Dimension]] = {d.id: d for d in DIMENSIONS}


def dimension(dimension_id: str) -> Dimension:
    try:
        return _BY_ID[dimension_id]
    except KeyError as e:
        raise FilterError(f"unknown filter dimension {dimension_id!r}", 1) from e


def recognise(node: object) -> tuple[str, object] | None:
    """``(dimension_id, value)`` for a clause a picker can edit, else None."""
    for dim in DIMENSIONS:
        value = dim.match(node)
        if value is not None:
            return dim.id, value
    return None


def note_kinds() -> Sequence[str]:
    return sorted(NOTE_KINDS)
