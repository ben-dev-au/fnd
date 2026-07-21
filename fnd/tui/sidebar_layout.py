"""Dynamic height allocation for the stacked sidebar panels.

The left column holds three panels — Results, Collections, Filters — sharing a
fixed height. A static CSS split can't be both content-tight (no empty space
when a panel is short) and priority-aware (Results dominates when long): CSS
can express "size to content" or "take the leftover", never "content when
short, lion's share when long". This module computes that allocation from live
content demand, so the split re-derives whenever rows, sections, or panels
expand or collapse.

Policy, in priority order (Results first):
  * Everything fits          → each panel gets exactly its content height and
                               the leftover pools in the priority panel.
  * Contention (demand > H)  → the priority panel is capped at ``SOFT_CAP`` of
                               the height (so the others aren't starved to
                               their headers), the remainder is split between
                               the rest in proportion to demand with a small
                               floor each, and anything past a panel's share
                               scrolls inside its own tree. Unused secondary
                               space is handed back to the priority panel.

The function is pure — it takes measured demands and returns cell heights — so
the policy is tested without a running app.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["SECONDARY_MIN", "SOFT_CAP", "Panel", "allocate"]

# Under contention the priority (Results) panel is capped here so the secondary
# panels keep usable room rather than collapsing to their header rows.
SOFT_CAP = 0.6
# Every expanded secondary panel keeps at least this many rows (header + a
# glimpse of content) before it starts scrolling.
SECONDARY_MIN = 3


@dataclass(frozen=True)
class Panel:
    """One sidebar panel's inputs for allocation.

    ``demand`` is the full box height the panel would occupy to show all its
    content (content rows + borders + any docked bar). ``header`` is the fixed
    box height it shows while collapsed-to-header.
    """

    key: str
    demand: int
    collapsed: bool
    header: int


def allocate(avail: int, panels: list[Panel]) -> dict[str, int]:
    """Return a ``{panel.key: height_in_cells}`` allocation.

    ``panels`` is in priority order (highest first). ``avail`` is the column's
    inner height. Collapsed panels are pinned to their header height and take
    no part in the flex; the expanded panels share what remains.
    """
    heights: dict[str, int] = {}
    fixed = 0
    expanded: list[Panel] = []
    for p in panels:
        if p.collapsed:
            heights[p.key] = p.header
            fixed += p.header
        else:
            expanded.append(p)
    if not expanded:
        return heights

    room = max(0, avail - fixed)
    demand = {p.key: max(1, p.demand) for p in expanded}
    total = sum(demand.values())

    if total <= room:
        # Fits: each panel exactly its content; the priority panel soaks the
        # slack so the column has no floating gap at the bottom.
        for p in expanded:
            heights[p.key] = demand[p.key]
        heights[expanded[0].key] += room - total
        return heights

    # Contention: protect (but don't inflate) the priority panel, then share
    # the rest by demand.
    priority = expanded[0]
    secondaries = expanded[1:]
    prio_h = min(demand[priority.key], max(1, round(room * SOFT_CAP)))
    remainder = room - prio_h
    sec_h = _split_by_demand(secondaries, remainder, demand)

    # Hand any secondary slack back to the priority panel (it wanted more).
    unused = remainder - sum(sec_h.values())
    if unused > 0:
        prio_h = min(demand[priority.key], prio_h + unused)

    heights[priority.key] = prio_h
    heights.update(sec_h)
    return heights


def _split_by_demand(secondaries: list[Panel], room: int, demand: dict[str, int]) -> dict[str, int]:
    """Share ``room`` across ``secondaries`` max-min fairly: every panel keeps
    ``SECONDARY_MIN`` where possible, then small demands are satisfied in full
    before a hungry panel takes the rest, so a short list never scrolls beside
    a long one while there is room. Content past a panel's share scrolls inside
    its own tree."""
    if not secondaries:
        return {}
    if room <= 0:
        return {s.key: 0 for s in secondaries}

    heights = {s.key: min(demand[s.key], SECONDARY_MIN) for s in secondaries}
    if sum(heights.values()) >= room:
        return _proportional(secondaries, room, demand)

    remaining = room - sum(heights.values())
    active = [s for s in secondaries if heights[s.key] < demand[s.key]]
    while active and remaining > 0:
        share = remaining // len(active)
        if share == 0:
            # A handful of rows left over — hand them to the hungriest panels.
            for s in sorted(active, key=lambda s: demand[s.key] - heights[s.key], reverse=True):
                if remaining <= 0:
                    break
                heights[s.key] += 1
                remaining -= 1
            break
        still_hungry: list[Panel] = []
        for s in active:
            give = min(share, demand[s.key] - heights[s.key])
            heights[s.key] += give
            remaining -= give
            if heights[s.key] < demand[s.key]:
                still_hungry.append(s)
        active = still_hungry
    return heights


def _proportional(secondaries: list[Panel], room: int, demand: dict[str, int]) -> dict[str, int]:
    """Fallback when ``room`` can't cover even the per-panel floors: hand out
    the little there is in proportion to demand, at least one row each."""
    total = sum(demand[s.key] for s in secondaries) or 1
    heights = {s.key: max(1, room * demand[s.key] // total) for s in secondaries}
    over = sum(heights.values()) - room
    for s in secondaries:
        if over <= 0:
            break
        take = min(heights[s.key] - 1, over)
        heights[s.key] -= take
        over -= take
    return heights
