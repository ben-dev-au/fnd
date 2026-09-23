"""Keep a tri-state marker's colour on the row the cursor is sitting on."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fnd.tui.results_labels import reapply_state_marker

if TYPE_CHECKING:
    from rich.style import Style
    from rich.text import Text
    from textual.widgets.tree import TreeNode

__all__ = ["StateMarkerLabel"]


class StateMarkerLabel:
    """Mixin for a ``Tree`` whose rows carry ``●``/``⊘``/``◐`` markers.

    Textual's cursor component style is stylised over the whole label after
    the label's own spans, so without this the marker reads plain on the
    highlighted row, the one place a user is certain to be looking.
    """

    def render_label(self, node: TreeNode[Any], base_style: Style, style: Style) -> Text:
        rendered = super().render_label(node, base_style, style)  # type: ignore[misc]
        return reapply_state_marker(rendered, node._label)
