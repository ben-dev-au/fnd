"""The one thing every filter compiles to.

A :class:`Rule` pairs a compiled predicate with the two pieces of policy the
strict-null evaluator cannot express: which kinds it applies to, and what an
unavailable fact means. Both matter at index time and neither exists at query
time, where the filter's effect is already baked into the index.
"""

from __future__ import annotations

import contextlib
import datetime as dt
from collections.abc import Iterable, Sequence
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


#: What the tag fields accept. ``__post_init__`` normalises a bare sequence
#: into the keyed form, so the annotation has to admit both or every caller
#: passing the documented shorthand is a type error.
TagSelection = dict[str, tuple[str, ...]] | Sequence[str]


@dataclass(frozen=True, slots=True)
class Rule:
    """One compiled clause plus its scope and unknown-value policy."""

    predicate: Predicate
    text: str
    facts: frozenset[str] = field(default_factory=frozenset)
    applies_to: frozenset[str] | None = None
    needs_frontmatter: bool = False
    """This rule asks about frontmatter, so a file carrying a block is judged
    whatever its kind. Frontmatter is not a Markdown-only convention.

    It does NOT excuse a file that has no block: a note with none is exactly
    the file a rule about ``Course`` is there to exclude, and skipping it made
    "index this course" mean "index everything except other courses".
    """
    unknown: Unknown = Unknown.PASS

    def passes(self, facts: FileFacts) -> bool:
        if not self._in_scope(facts):
            return True
        if self.unknown is Unknown.PASS and self._has_unknown(facts):
            return True
        return self.predicate(facts)

    def _in_scope(self, facts: FileFacts) -> bool:
        """A rule scoped to kinds ignores every other kind, so a frontmatter
        predicate cannot silently drop the PDFs it was never about; with
        ``needs_frontmatter``, it still judges any file that carries a block."""
        if self.applies_to is not None:
            try:
                kind = facts["file.kind"]
            except KeyError:
                kind = None
            if kind in self.applies_to:
                return True
        if self.needs_frontmatter:
            return facts.has_frontmatter()
        return self.applies_to is None

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


def spec_from_resolved(resolved: object) -> FilterSpec:
    """The canonical spec for a source's resolved filters.

    `FilterSpec` reclassifies an expression naming only frontmatter fields as
    `frontmatter`, so reading the config's own value instead of building this
    dropped such a rule from both paths and it filtered nothing at all.
    """
    from fnd.filters.dimensions import tag_selection

    return FilterSpec(
        kinds=tuple(resolved.kinds),  # type: ignore[attr-defined]
        include_tags=tag_selection(resolved.include_tags),  # type: ignore[attr-defined]
        exclude_tags=tag_selection(resolved.exclude_tags),  # type: ignore[attr-defined]
        min_size=resolved.min_size,  # type: ignore[attr-defined]
        max_size=resolved.max_size,  # type: ignore[attr-defined]
        created_after=resolved.created_after,  # type: ignore[attr-defined]
        created_before=resolved.created_before,  # type: ignore[attr-defined]
        modified_after=resolved.modified_after,  # type: ignore[attr-defined]
        modified_before=resolved.modified_before,  # type: ignore[attr-defined]
        expression=resolved.expression or "",  # type: ignore[attr-defined]
        frontmatter=resolved.frontmatter or "",  # type: ignore[attr-defined]
    )


@dataclass(frozen=True, slots=True)
class FilterSpec:
    """The canonical filter set: what the UI edits, the config stores and the
    text form renders. Field names are dimension ids.

    ``raw`` holds clauses the text form carried that no dimension recognised,
    so round-tripping hand-written text never silently drops a condition.
    """

    kinds: tuple[str, ...] = ()
    # Keyed by tag-source id, the shape ``TagFilter`` already uses on the
    # query side: a Finder tag and a note's ``tags:`` entry sharing a word are
    # not the same statement about a file. ``tag_selection`` expands a bare
    # list into every source.
    include_tags: TagSelection = field(default_factory=dict)
    exclude_tags: TagSelection = field(default_factory=dict)
    min_size: int | None = None
    max_size: int | None = None
    created_after: dt.date | None = None
    created_before: dt.date | None = None
    modified_after: dt.date | None = None
    modified_before: dt.date | None = None
    frontmatter: str = ""
    expression: str = ""
    raw: tuple[str, ...] = ()

    @property
    def tag_includes(self) -> dict[str, tuple[str, ...]]:
        """``include_tags`` in its normalised form. The field admits the bare
        shorthand a config may write; readers want the keyed shape."""
        from fnd.filters.dimensions import tag_selection

        return tag_selection(self.include_tags)

    @property
    def tag_excludes(self) -> dict[str, tuple[str, ...]]:
        """``exclude_tags`` in its normalised form; see :attr:`tag_includes`."""
        from fnd.filters.dimensions import tag_selection

        return tag_selection(self.exclude_tags)

    def impossible_bounds(self) -> tuple[str, ...]:
        """Pairs that cannot both hold, so the set matches nothing.

        Decidable without touching a corpus, and worth saying: contradictory
        bounds are accepted everywhere and index nothing, silently.
        """
        pairs = (
            ("size", self.min_size, self.max_size, "smallest is above largest"),
            ("created", self.created_after, self.created_before, "starts after it ends"),
            ("modified", self.modified_after, self.modified_before, "starts after it ends"),
        )
        clashes = [
            f"{name}: {why}"
            for name, low, high, why in pairs
            if low is not None and high is not None and low > high  # type: ignore[operator]
        ]
        if self._every_required_tag_is_excluded():
            clashes.append("tags: every required tag is also excluded")
        return tuple(clashes)

    def _every_required_tag_is_excluded(self) -> bool:
        """Whether the tag rules can admit nothing at all.

        A file must carry a required tag to pass, and is dropped if it carries
        an excluded one, so when each required tag is excluded too, the set is
        empty however large the corpus.
        """
        includes = self.tag_includes
        if not any(includes.values()):
            return False
        excludes = self.tag_excludes
        return all(
            set(tags) <= set(excludes.get(source, ())) for source, tags in includes.items() if tags
        )

    def __post_init__(self) -> None:
        # Tags are a set per source, sorted so a spec survives its own text form.
        # A bare sequence claims every source, the rule the config and ``--tag``
        # already use.
        from fnd.filters.dimensions import tag_selection

        for name in ("include_tags", "exclude_tags"):
            object.__setattr__(self, name, tag_selection(getattr(self, name)))

        # An expression naming only frontmatter fields is a frontmatter rule: left
        # in ``expression`` it would strict-null every PDF out of the index, and
        # the same text would mean different things in the two fields.
        if self.expression and not self.frontmatter:
            with contextlib.suppress(Exception):
                from fnd.filter_dsl import parse as _parse
                from fnd.filter_dsl import referenced_fields

                fields = referenced_fields(_parse(self.expression))
                if fields and not any(f.startswith("file.") for f in fields):
                    object.__setattr__(self, "frontmatter", self.expression)
                    object.__setattr__(self, "expression", "")

        # The mirror: a frontmatter rule naming ``file.*`` is not one. Moved here,
        # where the user typed it, so `t` then save with no edit cannot move the
        # rule and change what is indexed.
        if self.frontmatter:
            with contextlib.suppress(Exception):
                from fnd.filters.text_form import _and_join, split_frontmatter

                scoped, rest = split_frontmatter(self.frontmatter)
                if rest:
                    object.__setattr__(self, "frontmatter", scoped)
                    object.__setattr__(self, "expression", _and_join(self.expression, rest))

    def is_empty(self) -> bool:
        return self == FilterSpec()
