"""How ready a file is, and what the arrow says about it.

Navigation cost is bimodal — a jump whose hits are captured is a blit, one
that has to build can be seconds — so the arrow is the only thing on screen
that says which is coming. These tests pin the classification and the
glyph/colour it maps to.
"""

from __future__ import annotations

from typing import Any

import pytest
from rich.style import Style

from fnd.tui.preview.warmth import WarmState, warm_state
from fnd.tui.widgets.results_tree import ResultsTree

# ── the classification ───────────────────────────────────────────


def state(seqs: list[int], held: set[int], *, warming: bool = False) -> WarmState:
    return warm_state(hit_seqs=seqs, is_captured=lambda s: s in held, warming=warming)


def test_every_listed_hit_captured_is_ready() -> None:
    assert state([3, 9], {3, 9}) is WarmState.READY


def test_one_missing_hit_is_not_ready() -> None:
    """Readiness is a promise about the whole file: Down/Up steps through
    every listed hit, so one uncaptured hit is one slow jump."""
    assert state([3, 9], {3}) is WarmState.COLD


def test_a_file_being_captured_reads_as_warming() -> None:
    assert state([3, 9], {3}, warming=True) is WarmState.WARMING


def test_warming_only_applies_while_something_is_missing() -> None:
    """Once every hit is held the file is READY even mid-pass — coverage may
    still be filling the gaps between matches, and that changes nothing the
    user can feel."""
    assert state([3, 9], {3, 9}, warming=True) is WarmState.READY


def test_a_file_with_no_listed_hits_is_ready() -> None:
    """There is nothing to jump to, so there is nothing to wait for.
    Reporting cold would put a warning on every zero-hit row about a jump
    that cannot happen."""
    assert state([], set()) is WarmState.READY


def test_whole_file_coverage_is_not_required() -> None:
    """Coverage's third tier fills the gaps between matches. Scroll-driven
    lazy mount already handles those fast enough to be imperceptible, so
    counting them would leave a file reading cold through ~30 s of idle work
    that changes nothing."""
    assert state([5], {5}) is WarmState.READY


# ── the arrow ────────────────────────────────────────────────────


class StubGroup:
    def __init__(self, parent_id: str) -> None:
        self.parent_id = parent_id


def file_node(tree: ResultsTree, parent_id: str, *, expanded: bool = False) -> Any:
    node = tree.root.add(parent_id, data={"kind": "file", "group": StubGroup(parent_id)})
    if expanded:
        node.expand()
    return node


@pytest.fixture
def tree() -> ResultsTree:
    return ResultsTree("Results", id="results_pane")


def icon_of(tree: ResultsTree, node: Any) -> str:
    return tree.render_label(node, Style(), Style()).plain[:2]


def test_a_ready_file_looks_exactly_like_an_ordinary_row(tree: ResultsTree) -> None:
    """Ready is the unremarkable state — it deliberately renders the stock
    arrow, so the indicator only ever draws the eye when a jump is going to
    cost something. Asserting the glyph alone would pass with the feature
    removed, so the contrast against an unready row is the real check.
    """
    node = file_node(tree, "a")
    tree.apply_warm_states({"a": WarmState.READY})
    ready = icon_of(tree, node)
    assert ready == ResultsTree.ICON_WARM == ResultsTree.ICON_NODE

    tree.apply_warm_states({"a": WarmState.COLD})
    assert icon_of(tree, node) != ready, "ready and unready are indistinguishable"


def test_an_unready_file_gets_the_hollow_arrow(tree: ResultsTree) -> None:
    """Shape, not just colour, carries the fact that changes a decision — at
    one cell a hue change alone is hard to read, and it has to survive a
    colourblind user or a low-contrast theme."""
    node = file_node(tree, "a")
    for unready in (WarmState.COLD, WarmState.WARMING):
        tree.apply_warm_states({"a": unready})
        assert icon_of(tree, node) == ResultsTree.ICON_BUILDING, unready


def test_the_arrow_still_shows_expansion(tree: ResultsTree) -> None:
    """It is the toggle first and a warmth indicator second; overloading it
    must not cost the meaning it already had."""
    node = file_node(tree, "a", expanded=True)
    tree.apply_warm_states({"a": WarmState.READY})
    assert icon_of(tree, node) == ResultsTree.ICON_WARM_EXPANDED
    tree.apply_warm_states({"a": WarmState.COLD})
    assert icon_of(tree, node) == ResultsTree.ICON_BUILDING_EXPANDED


def test_every_icon_is_the_width_the_tree_budgets_for(tree: ResultsTree) -> None:
    """The results pane's name budget is ``width - 2 - 7``, where the 2 is the
    stock toggle. A wider glyph would silently eat a cell of every filename."""
    from rich.cells import cell_len

    for icon in (
        ResultsTree.ICON_WARM,
        ResultsTree.ICON_WARM_EXPANDED,
        ResultsTree.ICON_BUILDING,
        ResultsTree.ICON_BUILDING_EXPANDED,
        ResultsTree.ICON_NODE,
        ResultsTree.ICON_NODE_EXPANDED,
    ):
        assert cell_len(icon) == 2, repr(icon)


def test_cold_and_warm_differ_in_hue_not_brightness(tree: ResultsTree) -> None:
    """A muted-vs-accent version of the same glyph was rejected: at one cell
    the two are hard to tell apart. Cold takes the score column's accent
    blue, warm keeps the theme accent."""
    node = file_node(tree, "a")
    tree.apply_warm_states({"a": WarmState.COLD})
    cold = tree.render_label(node, Style(), Style())
    assert ResultsTree.COLD_COLOUR in str(cold.spans[0].style)

    tree.apply_warm_states({"a": WarmState.WARMING})
    warming = tree.render_label(node, Style(), Style())
    assert ResultsTree.COLD_COLOUR not in str(warming.spans[0].style)
    assert cold.spans[0].style != warming.spans[0].style


def test_a_row_with_no_warmth_yet_renders_the_stock_arrow(tree: ResultsTree) -> None:
    """Before the first poll nothing is known. The tree must look exactly as
    it did rather than claim every file is cold."""
    node = file_node(tree, "a")
    assert icon_of(tree, node) == ResultsTree.ICON_NODE


def test_match_rows_are_left_alone(tree: ResultsTree) -> None:
    """They already carry a glyph for matches the preview cannot highlight;
    two unrelated marker systems on one row cost more than they tell you."""
    node = tree.root.add("f", data={"kind": "file", "group": StubGroup("a")})
    leaf = node.add_leaf("§ heading", data={"kind": "section", "hit": object()})
    tree.apply_warm_states({"a": WarmState.COLD})
    assert tree.render_label(leaf, Style(), Style()).plain.startswith("§")


# ── repaint discipline ───────────────────────────────────────────


def test_only_rows_that_changed_are_repainted(tree: ResultsTree) -> None:
    """Captures land at roughly ten a second. Repainting the list on every
    one would strobe it, so a row is touched only when its own state moves."""
    a = file_node(tree, "a")
    b = file_node(tree, "b")
    tree.apply_warm_states({"a": WarmState.COLD, "b": WarmState.COLD})
    before = (a._updates, b._updates)

    tree.apply_warm_states({"a": WarmState.READY, "b": WarmState.COLD})
    assert a._updates > before[0], "the row that changed was not repainted"
    assert b._updates == before[1], "an unchanged row was repainted"


def test_an_unchanged_map_does_no_work_at_all(tree: ResultsTree) -> None:
    node = file_node(tree, "a")
    tree.apply_warm_states({"a": WarmState.COLD})
    before = node._updates
    assert tree.apply_warm_states({"a": WarmState.COLD}) is False
    assert node._updates == before


def test_a_styled_label_does_not_displace_the_icon_span(tree: ResultsTree) -> None:
    """The icon's style is inherited from the stock render, read off the first
    span. Real file labels are styled Text — the score column is coloured and
    the whole row is dimmed — so if a label's own spans could land first, the
    icon would inherit the wrong style and lose its toggle meta with it.
    """
    from rich.text import Text

    label = Text("report.pdf", style="dim")
    label.append("  0.87", "bold #9ece6a")
    node = tree.root.add(label, data={"kind": "file", "group": StubGroup("a")})
    tree.apply_warm_states({"a": WarmState.COLD})

    rendered = tree.render_label(node, Style(color="white"), Style())
    icon_span = rendered.spans[0]
    assert icon_span.start == 0
    assert icon_span.end == len(ResultsTree.ICON_BUILDING)
    assert isinstance(icon_span.style, Style)
    assert icon_span.style.meta.get("toggle") is True, "the arrow stopped being clickable"


def test_the_icon_keeps_its_toggle_meta_in_every_state(tree: ResultsTree) -> None:
    """It is the expander first and a warmth indicator second. Rebuilding the
    prefix by hand would mean copying a constant out of a private Textual
    module; inheriting it means the click behaviour follows the library."""
    node = file_node(tree, "a")
    for state in WarmState:
        tree.apply_warm_states({"a": state})
        span = tree.render_label(node, Style(color="white"), Style()).spans[0]
        assert isinstance(span.style, Style)
        assert span.style.meta.get("toggle") is True, state
