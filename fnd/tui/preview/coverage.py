"""Which chunks to capture ahead, and in what order.

What the preview MOUNTS ahead is decided by file size: everything below the
landing when a file is inside ``FULLMOUNT_CHUNK_BUDGET``, and a ±7 window
otherwise. That has to stay — the in-file match count walks the mounted subtree,
so a file that stops being filled stops being counted in full — but it leaves the
large files with nothing. A 1018-chunk PDF mounts a window and throws it away on
the next jump, so every far jump rebuilds from source whether or not the target
was visited moments earlier.

Coverage fills that gap without touching what is mounted. What navigation
actually visits is the MATCHES, so those are captured ahead, under two rules:

* coverage fills a CACHE, not the pane. Mounting scattered hit chunks would put
  chunk 12 directly above chunk 20 with the document between them silently
  missing — the mounted set has to stay contiguous, because lazy mount only ever
  fills at its edges. A capture costs 44.5 KB and NO arrange time, which is what
  lets the cache hold a scattered set spanning several files that the DOM could
  not.
* the cache is bounded in BYTES (see :mod:`fnd.tui.preview.frozen_store`),
  because memory is the only thing it spends.

The scarce resource is neither memory nor DOM but TIME: captures run serially on
one off-screen host at roughly ten chunks a second. So order is the whole design,
and it is strictly by what a navigation is most likely to need next:

1. the current file's hit chunks, nearest first from the cursor;
2. the neighbouring files' hit chunks, outward from the cursor's place in the
   results list — this is the step that makes moving BETWEEN files served;
3. the current file's remaining chunks, and only once 1 and 2 are drained.

Tier 3 last is the point. Covering a file whole spends ~30 seconds of the one
serial host on chunks no jump lands on, while the neighbours get nothing — which
is precisely the buffer the cursor needs. It earns its place only as idle work.
"""

from __future__ import annotations

from collections.abc import Container as ContainerABC
from collections.abc import Iterable, Sequence

__all__ = ["coverage_targets", "filler_targets", "neighbour_order"]


def _nearest_first(wanted: Iterable[int], focus_idx: int, already: ContainerABC[int]) -> list[int]:
    return sorted((i for i in wanted if i not in already), key=lambda i: (abs(i - focus_idx), i))


def coverage_targets(
    *,
    total: int,
    focus_idx: int,
    hit_indices: Iterable[int],
    already: ContainerABC[int],
    margin: int,
    budget: int,
) -> list[int]:
    """Hit chunks plus ``margin`` either side, nearest to the focus first.

    Nearest-first because coverage is background work the user interrupts:
    whatever it managed before they moved on should be the part they were most
    likely to reach next. The margin gives a landing context above and below
    rather than a bare match with unbuilt neighbours.
    """
    if total <= 0 or budget <= 0:
        return []
    wanted: set[int] = set()
    for i in hit_indices:
        if 0 <= i < total:
            wanted.update(range(max(0, i - margin), min(total, i + margin + 1)))
    return _nearest_first(wanted, focus_idx, already)[:budget]


def filler_targets(
    *, total: int, focus_idx: int, already: ContainerABC[int], budget: int
) -> list[int]:
    """Everything not already held, nearest first — the idle-time tier.

    Only worth running once every hit of the current file AND its neighbours is
    captured. Its sole benefit is that a scroll into the gaps between matches
    finds them ready, and scroll-driven lazy mount is already fast enough that
    this is a small prize for the one serial resource coverage has.
    """
    if total <= 0 or budget <= 0:
        return []
    return _nearest_first(range(total), focus_idx, already)[:budget]


def neighbour_order(ids: Sequence[str], here: int, span: int) -> list[str]:
    """Files either side of ``here``, nearest first, alternating outward.

    Alternating rather than one side then the other: the user is as likely to
    press up as down, and covering three files below before the one immediately
    above would make half the navigations feel unhelped.
    """
    order: list[str] = []
    for offset in range(1, span + 1):
        for idx in (here - offset, here + offset):
            if 0 <= idx < len(ids) and idx != here:
                order.append(ids[idx])
    return order
