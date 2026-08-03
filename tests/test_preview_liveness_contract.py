"""Pin the Textual behaviour ``is_live`` depends on.

``fnd/tui/preview/liveness.py`` reads private Textual state (``_pruning`` /
``_closing`` / ``_closed``) via ``getattr(..., False)``. That default is
deliberate — it keeps a teardown race from raising — but it means a Textual
upgrade that renames or drops those attributes would silently degrade
``is_live`` back to a plain attachment check, which is exactly the bug the
module exists to prevent. A silent regression here is expensive: it took five
attempts to find the first time.

So these tests assert the *behaviour* rather than the attribute names: a
removed-but-not-yet-pruned widget must still look attached, and ``is_live`` must
still call it dead. If Textual ever makes removal synchronous the attachment
assertion fails and tells us the guards can be simplified; if it renames the
flags the ``is_condemned`` assertion fails and tells us to update the predicate.
Either way the change surfaces loudly instead of quietly re-opening the bug.
"""

from __future__ import annotations

import pytest
from textual.app import App
from textual.widgets import Static

from fnd.tui.preview.liveness import is_condemned, is_live


@pytest.mark.asyncio
async def test_remove_is_deferred_and_is_live_sees_through_it() -> None:
    """The precise behaviour the whole fix rests on: between ``remove()`` and
    the ``Prune`` message being processed, the widget still reports a parent —
    and ``is_live`` must not be fooled by it."""

    class _Probe(App[None]):
        def compose(self):  # type: ignore[no-untyped-def]
            yield Static("hello", id="victim")

    app = _Probe()
    async with app.run_test() as pilot:
        await pilot.pause()
        victim = app.query_one("#victim", Static)
        assert is_live(victim), "setup — a mounted widget is live"
        assert not is_condemned(victim)

        victim.remove()

        # The load-bearing assertion: removal has NOT taken effect yet.
        assert victim.parent is not None, (
            "Textual's remove() is no longer deferred — the liveness guards in "
            "fnd/tui/preview/liveness.py can be simplified, and this test should "
            "be updated deliberately rather than deleted"
        )
        assert is_condemned(victim), (
            "a queued Prune must read as condemned — liveness.py detects it via "
            "Textual's private _pruning / _closing / _closed, read with a False "
            "default, so a rename shows up HERE and nowhere else"
        )
        assert not is_live(victim), (
            "is_live must reject a condemned widget even while it still reports "
            "a parent — this is the window the blank-preview strand lived in"
        )

        # And once the Prune lands it is gone for real.
        for _ in range(6):
            await pilot.pause()
        assert victim.parent is None
        assert not is_live(victim)


@pytest.mark.asyncio
async def test_is_live_rejects_a_widget_whose_ancestor_left_the_tree() -> None:
    """``is_attached`` walks to the DOM root, so a child still parented to a
    removed container is correctly dead — the second case ``parent is not None``
    missed."""
    from textual.containers import Vertical

    class _Probe(App[None]):
        def compose(self):  # type: ignore[no-untyped-def]
            with Vertical(id="box"):
                yield Static("hello", id="leaf")

    app = _Probe()
    async with app.run_test() as pilot:
        await pilot.pause()
        box = app.query_one("#box", Vertical)
        leaf = app.query_one("#leaf", Static)
        assert is_live(leaf)

        box.remove()
        for _ in range(6):
            await pilot.pause()

        assert not is_live(leaf), "a widget under a removed ancestor is not live"
