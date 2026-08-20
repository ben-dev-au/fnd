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

from collections.abc import Callable, Iterable
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


def warm_state(
    *,
    hit_seqs: Iterable[int],
    is_captured: Callable[[int], bool],
    warming: bool,
) -> WarmState:
    """Classify one file.

    ``hit_seqs`` is the chunk sequence of every hit the results list can
    navigate to — the listed hits, not every textual match, because those are
    exactly what Down/Up step through and what coverage targets.

    A file with no listed hits is READY: there is nothing to jump to, so
    there is nothing to wait for. Reporting it cold would paint every
    zero-hit row as a warning about a jump that cannot happen.

    Readiness is judged on the HITS alone, never on whole-file coverage.
    Coverage's third tier fills the gaps between matches, but scroll-driven
    lazy mount already handles those fast enough that the user cannot tell —
    so counting them would leave a file showing cold for the ~30 s of idle
    work that changes nothing they will notice.
    """
    seqs = list(hit_seqs)
    if not seqs:
        return WarmState.READY
    if all(is_captured(seq) for seq in seqs):
        return WarmState.READY
    return WarmState.WARMING if warming else WarmState.COLD
