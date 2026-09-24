"""Policy tests for the dynamic sidebar height allocator.

The allocator is pure — measured demands in, cell heights out — so the whole
policy is pinned here without a running app. Panels are given in priority order
(Results first).
"""

from __future__ import annotations

from fnd.tui.sidebar_layout import (
    RESERVED_MIN,
    RESERVED_SHARE,
    SECONDARY_MIN,
    SOFT_CAP,
    Panel,
    allocate,
    reserved_demand,
)


def _panels(
    r: int, c: int, f: int, *, rc: bool = False, cc: bool = False, fc: bool = False
) -> list[Panel]:
    return [
        Panel("R", r, rc, 3),
        Panel("C", c, cc, 2),
        Panel("F", f, fc, 2),
    ]


def test_fits_content_sizes_each_and_pools_slack_in_priority() -> None:
    h = allocate(45, _panels(6, 10, 6))
    # Secondaries take exactly their content; Results absorbs the leftover so
    # the column has no floating gap.
    assert h["C"] == 10
    assert h["F"] == 6
    assert h["R"] == 45 - 10 - 6
    assert sum(h.values()) == 45


def test_short_priority_hands_space_to_hungry_secondaries() -> None:
    # The case a static 50% floor gets wrong: Results wants almost nothing but
    # the secondaries are long — the slack must flow to them, not sit empty in
    # Results.
    h = allocate(45, _panels(5, 42, 42))
    assert h["R"] == 5
    assert h["C"] > 15
    assert h["F"] > 15
    assert sum(h.values()) == 45


def test_contention_caps_priority_at_soft_cap() -> None:
    h = allocate(45, _panels(52, 42, 42))
    assert h["R"] == round(45 * SOFT_CAP)  # 27
    assert h["C"] >= SECONDARY_MIN
    assert h["F"] >= SECONDARY_MIN
    assert sum(h.values()) == 45


def test_priority_reclaims_unused_secondary_space() -> None:
    # Long Results, short secondaries: Results should exceed the soft cap by
    # taking the room the secondaries don't want.
    h = allocate(45, _panels(52, 6, 6))
    assert h["C"] == 6
    assert h["F"] == 6
    assert h["R"] == 45 - 12
    assert h["R"] > round(45 * SOFT_CAP)


def test_short_secondary_is_satisfied_before_a_long_one_scrolls() -> None:
    # Max-min fairness: a 6-row filter list beside a 42-row collections list
    # must be shown in full while collections scrolls — not both cut to equal
    # shares.
    h = allocate(45, _panels(52, 42, 6))
    assert h["F"] == 6, "short secondary should get its full demand"
    assert h["C"] < 42, "long secondary scrolls"
    assert sum(h.values()) == 45


def test_collapsed_panel_is_pinned_to_its_header_and_excluded() -> None:
    h = allocate(45, _panels(52, 42, 42, rc=True))
    assert h["R"] == 3  # header only
    # The two expanded panels share everything but the header.
    assert h["C"] + h["F"] == 45 - 3


def test_all_collapsed_returns_only_headers() -> None:
    h = allocate(45, _panels(52, 42, 42, rc=True, cc=True, fc=True))
    assert h == {"R": 3, "C": 2, "F": 2}


def test_tiny_terminal_still_gives_priority_the_majority() -> None:
    h = allocate(16, _panels(52, 42, 42))
    assert h["R"] >= 8  # ~>=half of 16
    assert h["C"] >= 1
    assert h["F"] >= 1
    assert sum(h.values()) == 16


def test_never_over_allocates_the_column() -> None:
    # Fuzz a spread of demands/sizes; the allocation must never exceed avail.
    # Includes tiny columns (1-9 rows) where a naive per-panel floor of 1 would
    # over-allocate — the case CodeRabbit caught that the >=10 sweep missed.
    for avail in (1, 2, 3, 5, 7, 9, 10, 16, 24, 45, 60):
        for r in (1, 5, 30, 200):
            for c in (1, 4, 40):
                for f in (1, 4, 40):
                    h = allocate(avail, _panels(r, c, f))
                    assert sum(h.values()) <= avail, (avail, r, c, f, h)
                    assert all(v >= 0 for v in h.values())


def test_tiny_column_stays_within_budget_with_all_expanded() -> None:
    # avail=3, three hungry panels: a floor of 1 each would sum to 3+ for the
    # secondaries alone and blow the budget. The total must still fit.
    for avail in (1, 2, 3, 4):
        h = allocate(avail, _panels(5, 40, 40))
        assert sum(h.values()) <= avail, (avail, h)


def test_second_panel_takes_priority_slack_when_results_collapsed() -> None:
    # With Results collapsed, Collections becomes the top expanded panel and
    # absorbs the leftover.
    h = allocate(45, _panels(52, 4, 4, rc=True))
    assert h["R"] == 3
    assert h["C"] == 45 - 3 - h["F"]  # collections pooled the slack


# ── reserved panel (Outline) ─────────────────────────────────────────────


def _with_outline(
    avail: int,
    r: int,
    c: int,
    f: int,
    *,
    rc: bool = False,
    oc: bool = False,
    cc: bool = False,
    fc: bool = False,
) -> list[Panel]:
    return [
        Panel("R", r, rc, 3),
        Panel("O", reserved_demand(avail), oc, 2, reserved=True),
        Panel("C", c, cc, 2),
        Panel("F", f, fc, 2),
    ]


def test_reserved_demand_is_a_share_of_the_column_with_a_floor() -> None:
    assert reserved_demand(45) == round(45 * RESERVED_SHARE)
    assert reserved_demand(4) == RESERVED_MIN


def test_reserved_panel_height_ignores_what_the_others_want() -> None:
    heights = {allocate(45, _with_outline(45, r, 6, 10))["O"] for r in (4, 30, 200)}
    assert heights == {reserved_demand(45)}


def test_reserved_panel_is_pinned_and_priority_soaks_the_slack() -> None:
    h = allocate(45, _with_outline(45, 6, 10, 6))
    assert h["O"] == reserved_demand(45)
    assert h["C"] == 10
    assert h["F"] == 6
    assert h["R"] == 45 - h["O"] - 16
    assert sum(h.values()) == 45


def test_collapsed_reserved_panel_keeps_only_its_header() -> None:
    h = allocate(45, _with_outline(45, 52, 42, 42, oc=True))
    assert h["O"] == 2
    assert sum(h.values()) == 45


def test_reserved_panel_yields_to_results_on_a_short_column() -> None:
    for avail in (6, 10, 16, 19):
        h = allocate(avail, _with_outline(avail, 52, 42, 42))
        assert h["R"] >= max(3, avail // 2), (avail, h)
        assert h["O"] < reserved_demand(avail), (avail, h)  # the clamp engaged
        assert sum(h.values()) <= avail, (avail, h)


def test_reserved_panel_leaves_every_other_panel_its_floor() -> None:
    # An 80x24 terminal leaves ~21 rows: the outline must not squeeze a short
    # Collections list to its borders.
    for avail in range(16, 41):
        for r in (40, 200):
            for c in (2, 5, 9):
                for f in (2, 5, 9, 20):
                    h = allocate(avail, _with_outline(avail, r, c, f))
                    assert h["C"] >= min(c, SECONDARY_MIN), (avail, r, c, f, h)
                    assert h["F"] >= min(f, SECONDARY_MIN), (avail, r, c, f, h)
                    assert sum(h.values()) <= avail, (avail, h)


def test_reserved_panel_alone_takes_the_whole_room() -> None:
    h = allocate(45, _with_outline(45, 52, 42, 42, rc=True, cc=True, fc=True))
    assert h["O"] == 45 - 3 - 2 - 2


def test_never_over_allocates_with_a_reserved_panel() -> None:
    for avail in (1, 2, 3, 5, 7, 9, 10, 16, 24, 45, 60):
        for r in (1, 5, 30, 200):
            for c in (1, 4, 40):
                for collapsed in (False, True):
                    if collapsed and avail < 3:
                        continue  # a header plus the priority floor outgrow the column
                    h = allocate(avail, _with_outline(avail, r, c, c, oc=collapsed))
                    assert sum(h.values()) <= avail, (avail, r, c, h)
                    assert all(v >= 0 for v in h.values())
