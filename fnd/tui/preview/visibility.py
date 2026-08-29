"""Show and hide preview containers without restyling everything inside them.

``add_class``/``remove_class`` call ``App.update_styles``, which walks EVERY
descendant of the node and re-runs selector matching on each one. A preview
container holds a mounted document — hundreds of widgets — and it is toggled on
every activation and every swap.

Measured on the real corpus, sampling the main thread whenever the event loop
went away: **108 of 110 samples were inside ``Stylesheet.apply``**, and over half
of those were reached through this descendant walk. It was the largest single
cost left in a navigation.

Three conditions have to hold before skipping the walk is sound, and only the
first is about selectors:

1. The class must never appear as an ANCESTOR in a selector (``.-hidden Foo``),
   or descendants really would match differently.
2. It must not change the geometry descendants are laid out against. Styling and
   layout are not separable here — the walk is also what re-arranges the subtree.
3. It must not change a rule descendants' cached styles are computed from —
   and ``-pre-reveal`` does, so this one is handled rather than relied on.

Textual computes a widget's ``visual_style`` by walking its ANCESTORS for
``opacity``, ``background`` and friends, then caches the result against that
widget's OWN cache key, which an ancestor-only restyle never moves. Under
``opacity: 0`` an ancestor contributes no background at all, so a callout built
behind ``-pre-reveal`` kept painting its text on the bare pane colour for the
life of the container, while the blockquote around it — restyled directly —
showed the tint. Reveal therefore clears those caches itself: a cache clear and
a repaint per descendant, no selector matching. Measured over real navigations,
once per reveal — 40 widgets / 0.9ms median, 571 / 12.9ms worst.

``-hidden`` and ``-pre-reveal`` satisfy the first two: each is declared only on
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

Conditions 1 and 3 are enforced rather than trusted: ``tests/test_preview_visibility.py``
fails if a rule appears that uses either class as an ancestor, or if a shortcut
class sets an inherited rule that ``ANCESTOR_RULES`` does not list.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from textual.dom import DOMNode

__all__ = [
    "ANCESTOR_RULES",
    "NODE_ONLY_CLASSES",
    "set_node_class",
    "set_preview_visibility",
]

# The classes this module shortcuts, and the ones the invariant test checks.
# Both are declared in PreviewContainer.DEFAULT_CSS. Adding one here means
# asserting it meets ALL THREE conditions in the module docstring.
NODE_ONLY_CLASSES = ("-hidden", "-pre-reveal", "is-loading")

# Rules a descendant's cached style is computed from, so a change to one on this
# node invalidates every cache below it. See the third condition above.
ANCESTOR_RULES = (
    "opacity",
    "background",
    "background_tint",
    "color",
    "text_style",
    "auto_color",
)


def _ancestor_signature(node: DOMNode) -> tuple[object, ...]:
    """The node's current values for every rule its descendants cache."""
    get_rule = node.styles.get_rule
    return tuple(get_rule(rule) for rule in ANCESTOR_RULES)


def _restyle_node(node: DOMNode, before: tuple[object, ...]) -> None:
    """Apply ``node``'s own styles, then bust descendant caches if they moved."""
    node.app.stylesheet.update_nodes((node,), animate=False)
    if _ancestor_signature(node) == before:
        return
    for descendant in node.query("*"):
        descendant.notify_style_update()
        descendant.refresh()


def set_node_class(node: DOMNode, name: str, present: bool) -> None:
    """Add or remove one node-only class, restyling just ``node``."""
    if present == node.has_class(name):
        return
    if present:
        node.add_class(name, update=False)
    else:
        node.remove_class(name, update=False)
    _restyle_node(node, _ancestor_signature(node))


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
    _restyle_node(node, _ancestor_signature(node))
