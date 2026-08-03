"""Is this preview widget genuinely part of the DOM *right now*?

Textual removal is deferred. ``Widget.remove()`` calls ``App._prune``, which
posts a ``Prune`` message and sets ``_pruning``; only when that message is
processed does ``on_prune`` close the widget's message loop and detach it. In
between, the widget still reports a ``parent`` and still turns up in
``app.query()`` — it looks healthy and is already dead.

The preview pipeline used to ask ``widget.parent is None`` at each seam, which
is blind to that window, so a condemned container sailed through every guard:
the stranded-container sweep condemns it, the DOM-scan adopt picks it back up,
the mount sees a parent and skips its ``pane.mount()``, activates it, and the
queued ``Prune`` then tears it out — leaving ``active`` pointing at a widget
that is not in the tree. Blank pane, no self-heal. Re-attaching such a widget is
not a fix either: its message loop is already closing.

So the rule the whole subsystem now shares: **a condemned widget is dead —
never adopt it, never activate it, never cache it. Build a fresh one.**
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from textual.widget import Widget

__all__ = ["is_condemned", "is_live"]


def is_condemned(widget: Widget | None) -> bool:
    """True when ``widget`` is being torn down — its removal is queued or its
    message loop is closing — even though it may still report a parent."""
    if widget is None:
        return False
    return bool(
        getattr(widget, "_pruning", False)
        or getattr(widget, "_closing", False)
        or getattr(widget, "_closed", False)
    )


def is_live(widget: Widget | None) -> bool:
    """True when ``widget`` is attached to the running app's DOM and is not
    being torn down.

    Stronger than ``widget.parent is not None`` in two ways that both bite
    here: it rejects a widget whose ``Prune`` is merely queued (see module
    docstring), and — because ``is_attached`` walks to the DOM root — it also
    rejects one whose *ancestor* was removed, e.g. a container still parented to
    a ``#preview_pane`` that a screen teardown has already detached.
    """
    if widget is None:
        return False
    if is_condemned(widget):
        return False
    try:
        return bool(widget.is_attached)
    except Exception:
        # No active app / mid-teardown: nothing is live.
        return False
