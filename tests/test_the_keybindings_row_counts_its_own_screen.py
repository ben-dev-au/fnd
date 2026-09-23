"""The Keybindings row said "28 keys across 6 contexts" over a screen showing
55 keys across 9 sections.

It counted `keymap.bindings` (the action registry alone) while the sheet also
carries four static widget tables and lists a multi-pane action under each pane
it works in. The section count was a literal.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

from fnd.tui.menu import _provider_keybindings, _summary_keybindings


def _app() -> Any:
    return cast("Any", SimpleNamespace(_config=None))


def test_the_numbers_match_the_screen() -> None:
    items = _provider_keybindings(_app())
    rows = sum(1 for i in items if not i.is_header)
    sections = sum(1 for i in items if i.is_header)

    summary = _summary_keybindings(_app())

    assert f"{rows} keys" in summary, summary
    assert f"{sections} sections" in summary, summary


def test_it_counts_more_than_the_registry() -> None:
    """The premise: the sheet is bigger than the action registry."""
    from fnd.tui.actions import REGISTRY

    items = _provider_keybindings(_app())
    rows = sum(1 for i in items if not i.is_header)
    with_keys = sum(1 for a in REGISTRY if a.default_key is not None)

    assert rows > with_keys, (rows, with_keys)


def test_it_does_not_say_contexts() -> None:
    """Sections, not contexts: four of them are widget tables, not panes."""
    assert "contexts" not in _summary_keybindings(_app())
