"""The invariant that lets preview visibility skip the descendant restyle.

`fnd.tui.preview.visibility` shows and hides preview containers without walking
their contents to re-apply CSS. That is only sound while the two visibility
classes are never used as an ANCESTOR in a selector — the moment a rule like
``.-hidden Foo`` exists, descendants really can match differently and skipping
them would leave stale styling that no amount of staring at the CSS explains.

So the invariant is enforced rather than trusted, against the app's real
stylesheet, and a rule that breaks it fails here with a pointer to the reason.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from textual.css.model import CombinatorType, SelectorType

from fnd.tui import FNDApp
from fnd.tui.preview.visibility import (
    NODE_ONLY_CLASSES,
    set_node_class,
    set_preview_visibility,
)
from fnd.tui.widgets.preview_container import PreviewContainer


@pytest.mark.asyncio
async def test_the_visibility_classes_are_never_ancestor_selectors(tmp_index_dir: Path) -> None:
    app = FNDApp(index_dir=tmp_index_dir, initial_query="anything")
    async with app.run_test(size=(80, 24)):
        # Textual folds a widget's DEFAULT_CSS into the stylesheet only once an
        # instance exists, and the rules being guarded live on PreviewContainer.
        # Without this the scan runs over a stylesheet that does not contain
        # them and passes no matter what they say.
        await app.query_one("#preview_pane").mount(
            PreviewContainer(parent_doc_id="x", query_signature="y", total_chunks=1)
        )
        await app.workers.wait_for_complete()
        assert any(
            "PreviewContainer" in name
            for rule in app.stylesheet.rules
            for name in rule.selector_names
        ), "the container's own rules are not in the stylesheet; this scan would prove nothing"

        offenders: list[str] = []
        for rule in app.stylesheet.rules:
            for selector_set in rule.selector_set:
                selectors = selector_set.selectors
                for i, selector in enumerate(selectors):
                    if selector.type is not SelectorType.CLASS:
                        continue
                    if selector.name not in NODE_ONLY_CLASSES:
                        continue
                    # The list is flat: a selector with a SAME combinator
                    # belongs to the element before it, so the class styles an
                    # ANCESTOR exactly when some later selector opens a new
                    # element group.
                    if any(s.combinator is not CombinatorType.SAME for s in selectors[i + 1 :]):
                        offenders.append(f"{sorted(rule.selector_names)} -> .{selector.name}")
        assert not offenders, (
            f"a CSS rule now uses a preview visibility class as an ancestor "
            f"({offenders}). fnd/tui/preview/visibility.py skips the descendant "
            f"restyle on the assumption that none does, so that rule would not "
            f"apply. Either scope the rule to the container itself, or drop the "
            f"shortcut and take the restyle cost back."
        )


@pytest.mark.asyncio
async def test_setting_visibility_applies_the_style_to_the_node(tmp_index_dir: Path) -> None:
    """Skipping descendants must not mean skipping the node itself."""
    app = FNDApp(index_dir=tmp_index_dir, initial_query="anything")
    async with app.run_test(size=(80, 24)):
        container = PreviewContainer(parent_doc_id="x", query_signature="y", total_chunks=1)
        await app.query_one("#preview_pane").mount(container)
        await app.workers.wait_for_complete()

        set_preview_visibility(container, hidden=True)
        assert container.has_class("-hidden")
        assert container.styles.display == "none", (
            "the class was set but its style never applied — the node's own "
            "restyle was skipped along with its descendants"
        )

        set_preview_visibility(container, hidden=False)
        assert not container.has_class("-hidden")
        assert container.styles.display != "none", "hiding did not reverse"


@pytest.mark.asyncio
async def test_a_no_op_toggle_does_no_work(tmp_index_dir: Path) -> None:
    """Called every activation over every container, so the common case — the
    class is already right — must not reach the stylesheet at all."""
    app = FNDApp(index_dir=tmp_index_dir, initial_query="anything")
    async with app.run_test(size=(80, 24)):
        container = PreviewContainer(parent_doc_id="x", query_signature="y", total_chunks=1)
        await app.query_one("#preview_pane").mount(container)
        await app.workers.wait_for_complete()
        set_preview_visibility(container, hidden=True)

        calls = 0
        original = app.stylesheet.update_nodes

        def counting(nodes, animate=False):  # type: ignore[no-untyped-def]
            nonlocal calls
            calls += 1
            return original(nodes, animate=animate)

        app.stylesheet.update_nodes = counting  # type: ignore[method-assign]
        try:
            set_preview_visibility(container, hidden=True)
        finally:
            app.stylesheet.update_nodes = original  # type: ignore[method-assign]
        assert calls == 0, "a redundant toggle still restyled the node"


# Properties a shortcut class may set. Anything outside this changes the geometry
# descendants are laid out against, and the descendant walk is what re-arranges
# them — so skipping it would leave the subtree sized for the old layout. That is
# not hypothetical: `is-loading` used to set `scrollbar-size-vertical`, and
# shortcutting it left tables laid out at the pre-scrollbar width, so a match
# inside a tall table could not be scrolled to and the off-screen indicator never
# flipped.
#
# Scrollbar COLOURS are allowed and scrollbar SIZE is not, which is the whole
# distinction: repainting the bar moves nothing, resizing it re-wraps every line
# beneath it.
EXACT_ALLOWED = {"display", "opacity", "visibility"}
ALLOWED_PREFIXES = ("scrollbar_color", "scrollbar_background", "auto_scrollbar_")


def _is_geometric(name: str) -> bool:
    if name in EXACT_ALLOWED:
        return False
    return not name.startswith(ALLOWED_PREFIXES)


@pytest.mark.asyncio
async def test_shortcut_classes_change_visibility_only(tmp_index_dir: Path) -> None:
    app = FNDApp(index_dir=tmp_index_dir, initial_query="anything")
    async with app.run_test(size=(80, 24)):
        await app.query_one("#preview_pane").mount(
            PreviewContainer(parent_doc_id="x", query_signature="y", total_chunks=1)
        )
        await app.workers.wait_for_complete()

        checked = 0
        for rule in app.stylesheet.rules:
            names = {
                selector.name
                for selector_set in rule.selector_set
                for selector in selector_set.selectors
                if selector.type is SelectorType.CLASS
            }
            if not names & set(NODE_ONLY_CLASSES):
                continue
            checked += 1
            declared = set(rule.styles.get_rules())
            extra = {name for name in declared if _is_geometric(name)}
            assert not extra, (
                f"a shortcut class now sets {sorted(extra)}, which affects layout, "
                f"not just visibility. fnd/tui/preview/visibility.py skips the "
                f"descendant walk — and that walk is what re-arranges the subtree — "
                f"so descendants would keep their old geometry. Either drop the "
                f"class from NODE_ONLY_CLASSES, or move the property elsewhere."
            )
        assert checked, "no rules matched the shortcut classes; this scan proved nothing"


def test_the_shortcut_never_asks_for_a_subtree_restyle() -> None:
    """The invariant the whole module exists for, and the one nothing checked.

    Dropping ``update=False`` costs nothing visible: the class still lands, the
    style still applies, the no-op still no-ops — every other test here passes.
    What comes back is `App.update_styles` walking hundreds of descendants on
    every activation, which is the 108-of-110-samples cost this module removed.
    """
    from typing import cast

    from textual.dom import DOMNode

    from tests._preview_fakes import FakeContainer

    fake = FakeContainer()
    # Duck-typed on purpose: the stub carries only the four members these two
    # functions touch, which is what makes the flag assertions below readable.
    node = cast("DOMNode", fake)

    set_preview_visibility(node, hidden=True, pre_reveal=True)
    assert fake.class_calls, "nothing was flipped; this would pass on a no-op stub"
    assert all(update is False for _, update in fake.class_calls), (
        f"a visibility flip asked Textual to restyle the subtree: {fake.class_calls}"
    )
    # Both classes changed, but the restyle is paid for ONCE.
    assert len(fake.app.stylesheet.updated) == 1, (
        f"expected a single node-only restyle, got {fake.app.stylesheet.updated}"
    )
    assert fake.app.stylesheet.updated[0] == (fake,), "restyled something other than the node"

    set_node_class(node, "is-loading", True)
    assert fake.class_calls[-1] == ("is-loading", False)
    assert len(fake.app.stylesheet.updated) == 2

    # A redundant flip reaches neither.
    before = len(fake.class_calls), len(fake.app.stylesheet.updated)
    set_preview_visibility(node, hidden=True)
    set_node_class(node, "is-loading", True)
    assert (len(fake.class_calls), len(fake.app.stylesheet.updated)) == before
