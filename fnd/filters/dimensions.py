"""The shared filter vocabulary.

One :class:`Dimension` per thing a user can filter on. Each knows how to
render its value as DSL text (for display) and compile it to a :class:`Rule`.
Query time reuses the same names and value vocabularies
(``fnd.vocabulary``) so a config ``kinds = ["pdf"]`` and a ``--kind pdf`` flag
mean the same thing; it keeps its own tantivy compiler, because size, globs and
ignore files have no index field to compile against.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Protocol

from fnd.filter_dsl import FilterError, compile_filter, referenced_fields
from fnd.filter_dsl import parse as parse_dsl
from fnd.filters.model import Rule
from fnd.kinds import KINDS_IN_CATEGORY
from fnd.tags import normalise_tag

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

    def rule(self, value: object) -> Rule | None: ...


@dataclass(frozen=True, slots=True)
class _ListDimension:
    """``file.kind in ['pdf','md']`` — a set membership over a scalar fact."""

    id: str
    fact: str

    def render(self, value: object) -> str:
        values = _as_list(value)
        return f"{self.fact} in [{', '.join(_quote(v) for v in values)}]"

    def rule(self, value: object) -> Rule | None:
        values = _as_list(value)
        if not values:
            return None
        wanted = {str(v) for v in values}
        fact = self.fact

        def predicate(facts: Mapping[str, object]) -> bool:
            actual = facts.get(fact)
            return isinstance(actual, str) and actual in wanted

        return Rule(predicate=predicate, text=self.render(values), facts=frozenset({self.fact}))


@dataclass(frozen=True, slots=True)
class _TagExcludeDimension:
    """``NOT ('no_index' in file.tags.all)`` — one clause per excluded tag.

    Fail-open: a source that cannot answer contributes no tags, so an
    unreadable xattr or an unfetched note is indexed rather than dropped.
    """

    id: str
    fact: str

    def render(self, value: object) -> str:
        values = _as_list(value)
        return " AND ".join(f"NOT ({_quote(v)} in {self.fact})" for v in values)

    def rule(self, value: object) -> Rule | None:
        values = _as_list(value)
        if not values:
            return None
        excluded = {normalise_tag(str(v)) for v in values if normalise_tag(str(v))}
        if not excluded:
            return None
        fact = self.fact

        def predicate(facts: Mapping[str, object]) -> bool:
            actual = facts.get(fact)
            if not isinstance(actual, (list, tuple, set, frozenset)):
                return True
            return not (excluded & {str(t) for t in actual})

        return Rule(predicate=predicate, text=self.render(values), facts=frozenset({self.fact}))


@dataclass(frozen=True, slots=True)
class _TagIncludeDimension:
    """``'a' in file.tags.all OR 'b' in file.tags.all`` — carry at least one.

    Strict, unlike its exclude counterpart: "only files tagged X" cannot be
    satisfied by a file whose tags nothing could read.
    """

    id: str
    fact: str

    def render(self, value: object) -> str:
        values = _as_list(value)
        return " OR ".join(f"{_quote(v)} in {self.fact}" for v in values)

    def rule(self, value: object) -> Rule | None:
        values = _as_list(value)
        wanted = {normalise_tag(str(v)) for v in values if normalise_tag(str(v))}
        if not wanted:
            return None
        fact = self.fact

        def predicate(facts: Mapping[str, object]) -> bool:
            actual = facts.get(fact)
            if not isinstance(actual, (list, tuple, set, frozenset)):
                return False
            return bool(wanted & {str(t) for t in actual})

        return Rule(predicate=predicate, text=self.render(values), facts=frozenset({self.fact}))


@dataclass(frozen=True, slots=True)
class _ComparisonDimension:
    """A single ordered comparison against a fact: size or a date bound."""

    id: str
    fact: str
    op: str

    def render(self, value: object) -> str:
        return f"{self.fact} {self.op} {_quote(value)}"

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
    _TagIncludeDimension("include_tags", "file.tags.all"),
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


def note_kinds() -> Sequence[str]:
    return sorted(NOTE_KINDS)
