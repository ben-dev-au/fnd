"""What a source actually contains, for the filter pickers.

A source that has never been indexed has no catalogue to offer suggestions
from, which is exactly when you are setting its filters up. This samples the
tree instead: cheap facts for every candidate, and frontmatter only for note
kinds, under a wall-clock budget so a cloud-backed folder degrades to a
partial answer rather than a stall.
"""

from __future__ import annotations

import sys
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from fnd.filters.model import FileGate
from fnd.kinds import kind_for_suffix
from fnd.tags import TAG_PROVIDERS, TagContext, read_tags

__all__ = ["SourceSample", "sample_source"]

# A picker only needs enough to suggest; whole-corpus accuracy is the
# indexer's job, not this one's.
_DEFAULT_BUDGET_S = 1.5
_DEFAULT_MAX_FILES = 4000


@dataclass(slots=True)
class SourceSample:
    """Values seen while sampling, with counts so a picker can rank them."""

    kinds: dict[str, int] = field(default_factory=dict)
    # Of those, the ones this source's OTHER rules admit. The picker offers
    # every kind present; the count beside a kind must agree with the rule
    # printed under it, which said `no_index excluded` beside a raw `3`.
    # `gated` separates "no gate ran" from "the gate kept nothing", which an
    # empty dict alone cannot: a source whose every file is excluded would
    # otherwise fall back to the disk tally and reprint the lie.
    kinds_kept: dict[str, int] = field(default_factory=dict)
    gated: bool = False
    tags: dict[str, dict[str, int]] = field(default_factory=dict)
    frontmatter_keys: dict[str, int] = field(default_factory=dict)
    files_seen: int = 0
    truncated: bool = False

    def tags_for(self, source: str) -> list[tuple[str, int]]:
        """Tags from one provider, most common first."""
        counts = self.tags.get(source, {})
        return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))

    def ranked_kinds(self) -> list[tuple[str, int]]:
        return sorted(self.kinds.items(), key=lambda kv: (-kv[1], kv[0]))


def _note_kinds() -> frozenset[str]:
    from fnd.filters.dimensions import NOTE_KINDS

    return NOTE_KINDS


#: Both are on by default, so a sample that ignored them described a
#: source the indexer would never produce.
_DEFAULT_IGNORE_NAMES = (".gitignore", ".fndignore")


def sample_source(
    root: Path,
    *,
    budget_s: float = _DEFAULT_BUDGET_S,
    max_files: int = _DEFAULT_MAX_FILES,
    walk: Iterator[Path] | None = None,
    ignore_names: Sequence[str] | None = None,
    excludes: Sequence[str] | None = None,
    gate: FileGate | None = None,
) -> SourceSample:
    """Sample ``root`` for the values its filter pickers should offer.

    Stops at whichever of ``budget_s`` or ``max_files`` comes first, marking
    the result ``truncated`` so callers can say the list is partial rather
    than presenting it as complete.

    Ignore files and ``excludes`` are honoured, so the pickers offer what would
    actually be indexed: without them a `.fndignore`-d folder still contributed
    its types and tags, and a source naming `Archive/**` in its own excludes
    counted the files in it.
    """
    from fnd.file_facts import FileFacts
    from fnd.frontmatter import FrontmatterParseError, read_frontmatter_from_file
    from fnd.walk import walk as walk_files

    sample = SourceSample(gated=gate is not None)
    notes = _note_kinds()
    providers = [p for p in TAG_PROVIDERS.values() if p.available_on(sys.platform)]
    deadline = time.monotonic() + budget_s
    names = _DEFAULT_IGNORE_NAMES if ignore_names is None else tuple(ignore_names)
    paths = (
        walk
        if walk is not None
        else walk_files(roots=[root], ignore_names=names, excludes=list(excludes or ()) or None)
    )

    for path in paths:
        if sample.files_seen >= max_files or time.monotonic() > deadline:
            sample.truncated = True
            break
        sample.files_seen += 1
        kind = kind_for_suffix(path.suffix)
        if kind:
            sample.kinds[kind] = sample.kinds.get(kind, 0) + 1

        frontmatter: dict[str, object] | None = None
        if kind in notes:
            try:
                frontmatter = read_frontmatter_from_file(path)
            except (FrontmatterParseError, OSError, ValueError):
                frontmatter = None
            for key in frontmatter or {}:
                name = str(key)
                sample.frontmatter_keys[name] = sample.frontmatter_keys.get(name, 0) + 1

        for source, values in read_tags(
            TagContext(path=path, frontmatter=frontmatter), providers
        ).items():
            bucket = sample.tags.setdefault(source, {})
            for value in values:
                bucket[value] = bucket.get(value, 0) + 1

        if kind and gate is not None:
            # The frontmatter is already in hand; injecting it keeps the gate
            # from reading the file a second time.
            facts = FileFacts(
                path,
                root=root,
                read_frontmatter=lambda _p, fm=frontmatter: fm,
                tag_providers=providers,
            )
            if gate.passes(facts):
                sample.kinds_kept[kind] = sample.kinds_kept.get(kind, 0) + 1
    return sample
