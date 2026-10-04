"""Keep a collection mark's colour on the cursor row of a tree that is not focused."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fnd.tui.collection_marks import MARK_STYLES
from fnd.tui.freshness_view import MARKER_STYLE
from fnd.tui.results_labels import reapply_styles

if TYPE_CHECKING:
    from rich.style import Style
    from rich.text import Text
    from textual.widgets.tree import TreeNode

__all__ = ["CollectionMarkLabel"]


class CollectionMarkLabel:
    """Mixin: marks survive the cursor style unfocused; focused, the highlight wins over them."""

    def render_label(self, node: TreeNode[Any], base_style: Style, style: Style) -> Text:
        rendered = super().render_label(node, base_style, style)  # type: ignore[misc]
        if self.has_focus:  # type: ignore[attr-defined]
            return rendered
        return reapply_styles(rendered, node._label, MARK_STYLES | {MARKER_STYLE})
