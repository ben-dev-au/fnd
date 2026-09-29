"""Which collection a result came from: a muted colour and, past five, a shape.

Marks show only while more than one collection is searched, and no two marked
collections ever share one. They go to the searched collections in the order
the Collections panel lists them, so a mark holds until the scope changes.
When more are searched than there are marks, they go only to collections the
current results need marked (the sole searched home of some result file); any
beyond that stay unmarked, and the preview's bottom edge still names them.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from functools import cache

from rich.style import Style

#: Every pair at least 22 CIEDE2000 apart and at least 20 from the row text,
#: at one muted lightness and chroma; a sixth hue fell below both. Ordered so
#: the first colours handed out are the furthest apart.
PALETTE: tuple[str, ...] = (
    "#87be84",  # green
    "#e599ca",  # orchid
    "#f09b86",  # coral
    "#cbad6b",  # gold
    "#2fc3c5",  # teal
)

#: One cell each, distinct silhouettes; `▲` (matches above) and `★` (default
#: app) already mean something. The first tier is colour alone.
SHAPES: tuple[str, ...] = ("", "●", "■", "◆", "✚", "✖", "♣")


@dataclass(frozen=True, slots=True)
class Mark:
    colour: str
    shape: str = ""


MARKS: tuple[Mark, ...] = tuple(Mark(c, s) for s in SHAPES for c in PALETTE)


@cache
def mark_style(colour: str) -> Style:
    """Undimmed, so a mark keeps its colour on a dimmed row; minted once so it can be found again."""
    return Style(color=colour, dim=False)


#: Every style a mark is painted with, so a row style laid over one can be undone.
MARK_STYLES: frozenset[Style] = frozenset(mark_style(c) for c in PALETTE)


Memberships = Sequence[tuple[str, str]]


@dataclass(frozen=True, slots=True)
class CollectionMarks:
    """The mark of each collection, and which results should wear one."""

    marks: dict[str, Mark]
    full: frozenset[str]
    #: Ticked sources of each partly selected collection; its other sources are not searched.
    partial: Mapping[str, frozenset[str]] = field(default_factory=dict)

    @classmethod
    def build(
        cls,
        configured: Iterable[str],
        full: Iterable[str],
        partial: Mapping[str, Iterable[str]] | None = None,
        results: Iterable[Memberships] = (),
    ) -> CollectionMarks:
        """``results`` is each result file's memberships, consulted only past the last mark."""
        scope = cls(
            marks={},
            full=frozenset(full),
            partial={c: frozenset(s) for c, s in (partial or {}).items()},
        )
        pool = [name for name in sorted(configured) if scope.searches(name)]
        if len(pool) > len(MARKS):
            needed = {homes[0] for m in results if len(homes := scope.homes(m)) == 1}
            pool = [name for name in pool if name in needed]
        return replace(scope, marks=dict(zip(pool, MARKS, strict=False)))

    def searches(self, name: str) -> bool:
        return name in self.full or name in self.partial

    @property
    def active(self) -> bool:
        return len(self.full) + len(self.partial) > 1

    def homes(self, memberships: Memberships) -> tuple[str, ...]:
        """The searched collections that reach a file through a searched source."""
        out: dict[str, None] = {}
        for collection, source in memberships:
            ticked = self.partial.get(collection)
            if collection in self.full or (ticked is not None and source in ticked):
                out[collection] = None
        return tuple(out)

    def shown(self, memberships: Memberships) -> tuple[str, ...]:
        """The file's homes; empty while inactive."""
        return self.homes(memberships) if self.active else ()

    def mark(self, name: str) -> Mark | None:
        return self.marks.get(name) if self.active else None

    def file_mark(self, memberships: Memberships) -> Mark | None:
        """One home's mark; none for a file with several, which one mark cannot say."""
        homes = self.shown(memberships)
        return self.mark(homes[0]) if len(homes) == 1 else None


INACTIVE = CollectionMarks(marks={}, full=frozenset())
