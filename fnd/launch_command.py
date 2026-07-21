"""Serialise a search into a runnable ``fnd`` launch command.

A neutral domain module (no TUI, no CLI imports) that both surfaces depend
on, so neither the CLI nor the TUI reaches into the other. Three value
objects keep the serialise ↔ relaunch paths provable inverses:

- ``LaunchScope`` — the filter state expressible as ``fnd`` launch flags.
  The ``tui`` command both emits one (this module) and hydrates from one
  (``ScopeController``); a round-trip test asserts the symmetry.
- ``SearchSnapshot`` — the read-only projection the serializer consumes,
  built by ``ScopeController.snapshot`` so serialization never reaches into
  the controller's internals.
- ``LaunchCommand`` — the rendered command plus any lossy-conversion notes.
"""

from __future__ import annotations

import shlex
from collections.abc import Mapping
from dataclasses import dataclass, field


@dataclass(frozen=True)
class LaunchScope:
    """Filter state expressible as ``fnd`` launch flags. Collection scope
    travels on its own ``-c`` channel, so it is not carried here."""

    created: str | None = None
    modified: str | None = None
    kinds: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    not_tags: tuple[str, ...] = ()
    tag_match_all: bool = True

    def __bool__(self) -> bool:
        # tag_match_all is a mode, meaningless without tags — it never makes
        # an otherwise-empty scope "active".
        return bool(self.created or self.modified or self.kinds or self.tags or self.not_tags)


@dataclass(frozen=True)
class SearchSnapshot:
    """Read-only projection of the live search state the serializer needs."""

    query: str
    full_collections: tuple[str, ...] = ()
    partial_collections: tuple[str, ...] = ()
    filter_kinds: tuple[str, ...] = ()
    filter_date: str = "any"
    filter_created: str = "any"
    tag_include: Mapping[str, frozenset[str]] = field(default_factory=dict)
    tag_exclude: Mapping[str, frozenset[str]] = field(default_factory=dict)
    tag_match_all: bool = True


@dataclass
class LaunchCommand:
    """A rendered command plus notes about anything lost in translation."""

    command: str
    caveats: list[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        """True when there was nothing to copy (bare ``fnd``)."""
        return self.command == "fnd"


class LaunchCommandSerializer:
    """Builds a runnable ``fnd`` launch command from a ``SearchSnapshot``.

    One clause per private method, so the mapping from filter state to argv
    reads top-to-bottom; ``serialize`` assembles them."""

    def __init__(self, snapshot: SearchSnapshot) -> None:
        self._s = snapshot
        self._caveats: list[str] = []

    def serialize(self) -> LaunchCommand:
        # Builders return raw tokens; shlex.join quotes every one uniformly, so
        # a value with a space (a spaced collection name, an odd kind) can't
        # silently split the pasted command and no builder can forget to quote.
        args = [
            "fnd",
            *self._positional(),
            *self._collection_args(),
            *self._date_args(),
            *self._kind_args(),
            *self._tag_args(),
        ]
        return LaunchCommand(command=shlex.join(args), caveats=self._caveats)

    def _positional(self) -> list[str]:
        query = self._s.query.strip()
        return [query] if query else []

    def _collection_args(self) -> list[str]:
        names = list(self._s.full_collections)
        if self._s.partial_collections:
            # No CLI flag can express a partial (◐) source selection; widen to
            # the whole collection and tell the user it was broadened.
            names += [n for n in self._s.partial_collections if n not in names]
            self._caveats.append("partial source selections widened to full collection(s)")
        if not names:
            return []
        if len(names) > 1 and any("," in n for n in names):
            self._caveats.append("a collection name contains a comma; -c may be ambiguous")
        return ["-c", ",".join(names)]

    def _date_args(self) -> list[str]:
        out: list[str] = []
        if self._s.filter_created not in ("", "any"):
            out += ["--created", self._s.filter_created]
        if self._s.filter_date not in ("", "any"):
            out += ["--modified", self._s.filter_date]
        return out

    def _kind_args(self) -> list[str]:
        out: list[str] = []
        for k in self._s.filter_kinds:
            out += ["--kind", k]
        return out

    def _tag_args(self) -> list[str]:
        include = sorted(_flatten(self._s.tag_include))
        exclude = sorted(_flatten(self._s.tag_exclude))
        out: list[str] = []
        for value in include:
            out += ["--tag", value]
        for value in exclude:
            out += ["--not-tag", value]
        if include and not self._s.tag_match_all:
            out += ["--tag-match", "any"]
        return out


def _flatten(by_source: Mapping[str, frozenset[str]]) -> set[str]:
    """Union of tag values across every source (the CLI has no per-source
    tag flag, so provenance collapses to one set)."""
    return set[str]().union(*by_source.values())
