"""Which chunks to capture ahead, and in what order.

What the preview MOUNTS ahead is decided by file size: everything below the
landing when a file is inside ``FULLMOUNT_CHUNK_BUDGET``, and a ±7 window
otherwise. That has to stay — the in-file match count walks the mounted subtree,
so a file that stops being filled stops being counted in full — but it leaves the
large files with nothing. A 1018-chunk PDF mounts a window and throws it away on
the next jump, so every far jump rebuilds from source whether or not the target
was visited moments earlier.

Coverage fills that gap without touching what is mounted — with one exception.
A file the user has asked to warm WHOLE is mounted whole once its captures are
in, up to ``FULLWARM_MOUNT_MAX_CHUNKS``: capturing every chunk is only half of
what that promises, because the windowed fill adds three chunks per scroll event
and only when one fires, so at the top of the mounted region there is nothing
left to move and no more content arrives above.

What navigation actually visits is the MATCHES, so those are captured ahead,
under two rules:

* coverage fills a CACHE, not the pane. Mounting scattered hit chunks would put
  chunk 12 directly above chunk 20 with the document between them silently
  missing — the mounted set has to stay contiguous, because lazy mount only ever
  fills at its edges. A capture costs 44.5 KB and NO arrange time, which is what
  lets the cache hold a scattered set spanning several files that the DOM could
  not.
* the cache is bounded in BYTES (see :mod:`fnd.tui.preview.frozen_store`),
  because memory is the only thing it spends.

The scarce resource is neither memory nor DOM but TIME: captures run serially on
one off-screen host, measured at roughly six chunks a second. So order is the
whole design, and it is strictly by what a navigation is most likely to need:

1. every planned file's HIT chunks — the current file first, nearest to the
   cursor, then the neighbours outward, then the head of the result list;
2. one file WHOLE, if the user has asked for that (see
   ``PreviewPresenter.request_full_warm``);
3. the margin either side of the hits, which is context and yields to both.

Landings before context, across the whole plan. Taking one file's margins before
the next file has anything to land on cost 0.7/0.7/0.7/2.5/3.6s to five READY
files, against 0.6s for all five in this order.

A requested whole file sits between them: somebody is waiting on it, so it
outranks context — but not landings, which are what keeps every other file's
jump served. Measured, it costs nothing to be second: on a warmed session the
request starts within a capture, and only on a cold store does it wait.

A pass is only re-planned when it ENDS, so the margin walk also stands down for
a file the cursor has moved towards that the running plan never listed. Without
that, the file two below the cursor waited 31.0s for its first capture — 26.3s
of it the previous plan's margins, over files whose hits were already captured.

Whole-file coverage used to run automatically, after the rest drained: 438
captures over 80 seconds on one already-served file while 42 other result files
had nothing, to save a scroll lazy mount already handles. Being last did not
stop it taking everything, which is why it is now asked for rather than assumed
— and why what it costs goes to the user, who can watch it and stop it.

Moving the cursor re-plans; it does not demote. Tier 1's order is taken around
the cursor, so a move within the file invalidates the order but not the file,
which is still the only one that can serve the next keypress.
"""

from __future__ import annotations

from collections.abc import Container as ContainerABC
from collections.abc import Iterable, Sequence

__all__ = ["coverage_targets", "neighbour_order"]


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
