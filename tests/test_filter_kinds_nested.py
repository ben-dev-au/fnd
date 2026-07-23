"""The nested category → type File-type filter's tri-state marker + toggle logic.

Exercises the pure ScopeController helpers directly (bypassing __init__, which
needs a live app), mirroring how the Collections tree's tri-state is tested.
"""

from __future__ import annotations

from fnd.kinds import KINDS_IN_CATEGORY
from fnd.tui.scope_panel import ScopeController


def _controller() -> ScopeController:
    sc = ScopeController.__new__(ScopeController)
    sc.filter_kinds = []
    sc._present_kinds = None  # no scope pruning in this pure-logic unit test → all visible
    return sc


def test_category_marker_reflects_membership() -> None:
    sc = _controller()
    code = list(KINDS_IN_CATEGORY["code"])
    assert sc._kind_category_marker("code") == "○"  # nothing selected

    sc.filter_kinds = [code[0]]
    assert sc._kind_category_marker("code") == "◐"  # partial

    sc.filter_kinds = list(code)
    assert sc._kind_category_marker("code") == "●"  # all members


def test_toggle_category_selects_all_then_clears() -> None:
    sc = _controller()
    members = set(KINDS_IN_CATEGORY["data"])

    sc._toggle_kind_category("data")
    assert set(sc.filter_kinds) == members
    assert sc._kind_category_marker("data") == "●"

    sc._toggle_kind_category("data")  # full → clear
    assert not (set(sc.filter_kinds) & members)
    assert sc._kind_category_marker("data") == "○"


def test_individual_type_survives_category_partial() -> None:
    """Include a category, drop one type: the parent goes ◐ and the dropped
    type is gone — the 'include .py but not .cpp' requirement."""
    sc = _controller()
    sc._toggle_kind_category("code")
    sc.filter_kinds.remove("cpp")
    assert "python" in sc.filter_kinds
    assert "cpp" not in sc.filter_kinds
    assert sc._kind_category_marker("code") == "◐"

    # Toggling the partial category re-selects all members (incl. cpp).
    sc._toggle_kind_category("code")
    assert "cpp" in sc.filter_kinds
    assert sc._kind_category_marker("code") == "●"
