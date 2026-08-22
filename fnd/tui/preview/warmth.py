"""How ready a file is for an instant jump.

Coverage captures chunks ahead of the cursor so that navigating to a match
is a blit rather than a build (see :mod:`fnd.tui.preview.coverage`). That
makes navigation cost bimodal — a served jump is ~50 ms, an unserved one
can be seconds — and nothing on screen said which you were about to get.

One vocabulary, read by two places that must not drift: the results tree
paints it, and the progress line uses it to pick the plan for the
navigation it is about to report on. A file that is READY has no decode,
no mount and no build ahead of it, so pricing it with the same phases as
a cold one would misreport every jump.

Deliberately three states and not two. READY vs not is the fact that
changes what a jump costs, but the captures run SERIALLY on one
off-screen host, so exactly one file is ever WARMING — which makes it a
single marker walking outward from the cursor rather than noise across
the list, and it is what tells the user their wait is buying something.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Iterable
from enum import Enum

__all__ = ["WarmState", "warm_state"]


class WarmState(Enum):
    """Whether jumping into a file will be served from the capture store."""

    #: Nothing captured yet, and nothing being captured — this jump will build.
    COLD = "cold"
    #: Being captured right now. At most one file is ever in this state.
    WARMING = "warming"
    #: Every listed hit is captured; a jump to any of them is a blit.
    READY = "ready"
    #: Every capturable chunk is captured, not just the hits — so a scroll
    #: anywhere in the file is a blit too, not only a jump to a match.
    FULL = "full"

    @property
    def is_served(self) -> bool:
        """Whether a jump into this file is a blit rather than a build.

        Asked instead of comparing against READY: FULL is strictly warmer, and
        a caller testing for READY alone prices the warmest files as cold.
        """
        return self in (WarmState.READY, WarmState.FULL)


def warm_state(
    *,
    hit_seqs: Iterable[int],
    is_captured: Callable[[int], bool],
    warming: bool,
    unservable: Collection[int] = frozenset(),
    capturable_total: int = 0,
    captured_total: int = 0,
) -> WarmState:
    """Classify one file.

    ``hit_seqs`` is the chunk sequence of every hit the results list can
    navigate to — the listed hits, not every textual match, because those are
    exactly what Down/Up step through and what coverage targets.

    A file with no listed hits is READY: there is nothing to jump to, so
    there is nothing to wait for. Reporting it cold would paint every
    zero-hit row as a warning about a jump that cannot happen.

    Readiness is judged on the HITS alone, never on whole-file coverage. The
    chunks around a hit are captured too, but a jump lands on the hit and lazy
    mount fills the rest — counting them would leave a file showing cold for
    idle work the user cannot see.

    ``unservable`` holds hits coverage cannot capture at all: a plain-layout
    kind, or a chunk over the markdown renderer's size cap. Both take the flat
    path, which has no off-screen builder, so "captured" is unreachable for them
    and judging readiness over them can never come true. An excluded hit reports
    READY for the same reason a file with no hits does — there is nothing here
    for a jump to wait on.

    Per HIT, not per file: a 37-hit PDF with 36 hits captured reported cold
    indefinitely over one 46,266-character chunk against the 40,000 cap.

    FULL outranks READY because it is strictly stronger — every hit is covered
    by every chunk being covered. It is counted against the CAPTURABLE total,
    never the chunk count: a file whose chunks all take the flat path has no
    off-screen builder, and measuring it against zero would paint it fully warm
    having captured nothing.
    """
    if capturable_total > 0 and captured_total >= capturable_total:
        return WarmState.FULL
    seqs = [seq for seq in hit_seqs if seq not in unservable]
    if not seqs:
        return WarmState.READY
    if all(is_captured(seq) for seq in seqs):
        return WarmState.READY
    return WarmState.WARMING if warming else WarmState.COLD
