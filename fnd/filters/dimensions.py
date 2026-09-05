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
from fnd.tags import TAG_PROVIDERS, normalise_tag, source_tag_selection

__all__ = ["DIMENSIONS", "Dimension", "dimension", "rule_from_text"]

# Frontmatter is a Markdown convention: a .txt file has no YAML block, so a
# frontmatter predicate must not be evaluated against one — strict null would
# drop every plain-text file in the source.
NOTE_KINDS: Final[frozenset[str]] = frozenset({"md"} & set(KINDS_IN_CATEGORY.get("notes", ())))


def _as_list(value: object) -> list[object]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [v for v in value if v]
    return [value] if value else []


def tag_selection(value: object) -> dict[str, tuple[str, ...]]:
    """Config tags as ``{source: tags}``.

    A bare list claims every source — the rule ``source_tag_selection`` already
    applies to ``--tag`` on the query side — while a table names them:

        exclude_tags = ["no_index"]
        [defaults.filters.exclude_tags]
        os = ["archive"]
    """
    if isinstance(value, Mapping):
        return {
            str(source): tuple(sorted({normalise_tag(str(t)) for t in tags} - {""}))
            for source, tags in value.items()
            if source in TAG_PROVIDERS and tags
        }
    return {
        source: tuple(sorted(tags))
        for source, tags in source_tag_selection(
            (str(v) for v in _as_list(value)), TAG_PROVIDERS
        ).items()
    }


def _clauses(groups: dict[str, tuple[str, ...]]) -> list[tuple[str, str]]:
    """``(fact, tag)`` per clause, collapsing a tag held by every source.

    ``file.tags.all`` is how a user writes "wherever it came from", so a
    selection covering every source renders that way instead of one clause
    per source.
    """
    everywhere = set(next(iter(groups.values()), ())) if groups else set()
    for tags in groups.values():
        everywhere &= set(tags)
    if len(groups) < len(TAG_PROVIDERS):
        everywhere = set()
    out = [("file.tags.all", t) for t in sorted(everywhere)]
    for source in sorted(groups):
        out += [(tag_fact(source), t) for t in sorted(set(groups[source]) - everywhere)]
    return out


def tag_fact(source: str) -> str:
    """The fact a tag rule reads for one source."""
    return f"file.tags.{source}"


def _quote(value: object) -> str:
    if isinstance(value, str):
        # Escaped, not stripped: a tag may legitimately contain an
        # apostrophe, and this text is parsed back by the text form.
        return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"
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

    A tag may name the source it came from (``os:archive``), because a Finder
    tag and a note's ``tags:`` entry that happen to share a word are not the
    same statement about a file.

    Fail-open: a source that cannot answer contributes no tags, so an
    unreadable xattr or an unfetched note is indexed rather than dropped.
    """

    id: str

    def render(self, value: object) -> str:
        return " AND ".join(
            f"NOT ({_quote(t)} in {fact})" for fact, t in _clauses(tag_selection(value))
        )

    def rule(self, value: object) -> Rule | None:
        groups = tag_selection(value)
        if not groups:
            return None
        wanted = {tag_fact(s): set(tags) for s, tags in groups.items()}

        def predicate(file_facts: Mapping[str, object]) -> bool:
            for fact, tags in wanted.items():
                actual = file_facts.get(fact)
                if not isinstance(actual, (list, tuple, set, frozenset)):
                    continue
                if tags & {str(t) for t in actual}:
                    return False
            return True

        return Rule(predicate=predicate, text=self.render(value), facts=frozenset(wanted))


@dataclass(frozen=True, slots=True)
class _TagIncludeDimension:
    """``'a' in file.tags.all OR 'b' in file.tags.all`` — carry at least one.

    Strict, unlike its exclude counterpart: "only files tagged X" cannot be
    satisfied by a file whose tags nothing could read.
    """

    id: str

    def render(self, value: object) -> str:
        return " OR ".join(f"{_quote(t)} in {fact}" for fact, t in _clauses(tag_selection(value)))

    def rule(self, value: object) -> Rule | None:
        groups = tag_selection(value)
        if not groups:
            return None
        wanted = {tag_fact(s): set(tags) for s, tags in groups.items()}

        def predicate(file_facts: Mapping[str, object]) -> bool:
            for fact, tags in wanted.items():
                actual = file_facts.get(fact)
                if isinstance(actual, (list, tuple, set, frozenset)) and tags & {
                    str(t) for t in actual
                }:
                    return True
            return False

        return Rule(predicate=predicate, text=self.render(value), facts=frozenset(wanted))


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
        return _compile(text, needs_frontmatter=self.applies_to is not None)


def _compile(text: str, *, needs_frontmatter: bool = False) -> Rule:
    node = parse_dsl(text)
    return Rule(
        predicate=compile_filter(text),
        text=text,
        facts=referenced_fields(node),
        needs_frontmatter=needs_frontmatter,
    )


def rule_from_text(text: str, *, needs_frontmatter: bool = False) -> Rule:
    """Compile arbitrary DSL text into a rule. Raises :class:`FilterError`."""
    return _compile(text, needs_frontmatter=needs_frontmatter)


DIMENSIONS: Final[tuple[Dimension, ...]] = (
    _ListDimension("kinds", "file.kind"),
    _TagIncludeDimension("include_tags"),
    _TagExcludeDimension("exclude_tags"),
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
