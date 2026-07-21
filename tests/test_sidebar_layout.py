"""Policy tests for the dynamic sidebar height allocator.

The allocator is pure — measured demands in, cell heights out — so the whole
policy is pinned here without a running app. Panels are given in priority order
(Results first).
"""

from __future__ import annotations

from fnd.tui.sidebar_layout import SECONDARY_MIN, SOFT_CAP, Panel, allocate


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
    for avail in (10, 16, 24, 45, 60):
        for r in (1, 5, 30, 200):
            for c in (1, 4, 40):
                for f in (1, 4, 40):
                    h = allocate(avail, _panels(r, c, f))
                    assert sum(h.values()) <= avail, (avail, r, c, f, h)
                    assert all(v >= 0 for v in h.values())


def test_second_panel_takes_priority_slack_when_results_collapsed() -> None:
    # With Results collapsed, Collections becomes the top expanded panel and
    # absorbs the leftover.
    h = allocate(45, _panels(52, 4, 4, rc=True))
    assert h["R"] == 3
    assert h["C"] == 45 - 3 - h["F"]  # collections pooled the slack
