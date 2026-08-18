"""Show and hide preview containers without restyling everything inside them.

``add_class``/``remove_class`` call ``App.update_styles``, which walks EVERY
descendant of the node and re-runs selector matching on each one. A preview
container holds a mounted document — hundreds of widgets — and it is toggled on
every activation and every swap.

Measured on the real corpus, sampling the main thread whenever the event loop
went away: **108 of 110 samples were inside ``Stylesheet.apply``**, and over half
of those were reached through this descendant walk. It was the largest single
cost left in a navigation.

Two conditions have to hold before skipping the walk is sound, and the second is
easy to miss:

1. The class must never appear as an ANCESTOR in a selector (``.-hidden Foo``),
   or descendants really would match differently.
2. It must not change the geometry descendants are laid out against. Styling and
   layout are not separable here — the walk is also what re-arranges the subtree.

``-hidden`` and ``-pre-reveal`` satisfy both: each is declared only on
``PreviewContainer`` itself, and each toggles the container's own visibility
without resizing it.

``is-loading`` failed the second condition until it was changed to match: it
used to set ``scrollbar-size-vertical: 0``, removing the gutter and so changing
the pane's content width, and shortcutting it left tables laid out at the old
width — the match coordinate inside a tall table could not be resolved and the
off-screen indicator never flipped. It now hides the bar by COLOUR instead,
which leaves the geometry alone, so it qualifies. Condition 2 is what that
episode cost, and ``test_shortcut_classes_change_visibility_only`` is what stops
it recurring.

Condition 1 is enforced rather than trusted: ``tests/test_preview_visibility.py``
fails if a rule appears that uses either class as an ancestor.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from textual.dom import DOMNode

__all__ = ["NODE_ONLY_CLASSES", "set_node_class", "set_preview_visibility"]

# The classes this module shortcuts, and the ones the invariant test checks.
# Both are declared in PreviewContainer.DEFAULT_CSS. Adding one here means
# asserting it meets BOTH conditions in the module docstring.
NODE_ONLY_CLASSES = ("-hidden", "-pre-reveal", "is-loading")


def set_node_class(node: DOMNode, name: str, present: bool) -> None:
    """Add or remove one node-only class, restyling just ``node``."""
    if present == node.has_class(name):
        return
    if present:
        node.add_class(name, update=False)
    else:
        node.remove_class(name, update=False)
    node.app.stylesheet.update_nodes((node,), animate=False)


def set_preview_visibility(
    node: DOMNode,
    *,
    hidden: bool | None = None,
    pre_reveal: bool | None = None,
) -> None:
    """Set either or both visibility classes, restyling only ``node``.

    ``None`` leaves a class alone. Both are set before the single restyle, so a
    call that changes both costs one apply rather than two.
    """
    changed = False
    for name, wanted in (("-hidden", hidden), ("-pre-reveal", pre_reveal)):
        if wanted is None or wanted == node.has_class(name):
            continue
        if wanted:
            node.add_class(name, update=False)
        else:
            node.remove_class(name, update=False)
        changed = True
    if not changed:
        return
    # What ``update_styles`` would do for this one node, minus the walk.
    node.app.stylesheet.update_nodes((node,), animate=False)
