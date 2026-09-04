"""The one thing every filter compiles to.

A :class:`Rule` pairs a compiled predicate with the two pieces of policy the
strict-null evaluator cannot express: which kinds it applies to, and what an
unavailable fact means. Both matter at index time and neither exists at query
time, where the filter's effect is already baked into the index.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import Enum, auto

from fnd.file_facts import FileFacts
from fnd.filter_dsl import Predicate

__all__ = ["FileGate", "FilterSpec", "Rule", "Unknown"]


class Unknown(Enum):
    """What an unavailable fact means for a rule.

    ``PASS`` is the index-time default: ext4 reports no birth time, so a
    ``created`` rule that dropped on unknown would index nothing there.
    """

    PASS = auto()
    DROP = auto()


@dataclass(frozen=True, slots=True)
class Rule:
    """One compiled clause plus its scope and unknown-value policy."""

    predicate: Predicate
    text: str
    facts: frozenset[str] = field(default_factory=frozenset)
    applies_to: frozenset[str] | None = None
    unknown: Unknown = Unknown.PASS

    def passes(self, facts: FileFacts) -> bool:
        if not self._in_scope(facts):
            return True
        if self.unknown is Unknown.PASS and self._has_unknown(facts):
            return True
        return self.predicate(facts)

    def _in_scope(self, facts: FileFacts) -> bool:
        """A rule scoped to kinds ignores every other kind, so a frontmatter
        predicate cannot silently drop the PDFs it was never about."""
        if self.applies_to is None:
            return True
        try:
            kind = facts["file.kind"]
        except KeyError:
            return False
        return kind in self.applies_to

    def _has_unknown(self, facts: FileFacts) -> bool:
        return any(facts.is_unknown(name) for name in self.facts)


@dataclass(frozen=True, slots=True)
class FileGate:
    """Every rule must pass. Empty admits everything."""

    rules: tuple[Rule, ...] = ()

    def passes(self, facts: FileFacts) -> bool:
        return all(rule.passes(facts) for rule in self.rules)

    def __bool__(self) -> bool:
        return bool(self.rules)

    @classmethod
    def of(cls, rules: Iterable[Rule]) -> FileGate:
        return cls(tuple(rules))


@dataclass(frozen=True, slots=True)
class FilterSpec:
    """The canonical filter set: what the UI edits, the config stores and the
    text form renders. Field names are dimension ids.

    ``raw`` holds clauses the text form carried that no dimension recognised,
    so round-tripping hand-written text never silently drops a condition.
    """

    kinds: tuple[str, ...] = ()
    include_tags: tuple[str, ...] = ()
    exclude_tags: tuple[str, ...] = ()
    min_size: int | None = None
    max_size: int | None = None
    created_after: dt.date | None = None
    created_before: dt.date | None = None
    modified_after: dt.date | None = None
    modified_before: dt.date | None = None
    frontmatter: str = ""
    expression: str = ""
    raw: tuple[str, ...] = ()

    def is_empty(self) -> bool:
        return self == FilterSpec()
