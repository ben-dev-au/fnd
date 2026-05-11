"""Phase 3 (Settings UX redesign) — visual foundation tests."""

from __future__ import annotations


def test_indexer_filetypes_exposed_and_complete() -> None:
    """Spec: Add Collection wizard › Includes — file types come from a
    single source of truth, not hardcoded in two places."""
    from acorn.config import INDEXER_FILETYPES

    # Map of extension -> human label. Order is the order the picker shows.
    assert tuple(INDEXER_FILETYPES) == ("md", "pdf", "docx", "pptx", "txt")
    assert INDEXER_FILETYPES["md"] == "Markdown (.md)"
    assert INDEXER_FILETYPES["pdf"] == "PDF (.pdf)"


def test_f3_no_longer_in_keymap() -> None:
    """Spec: Locked decisions — F3 dropped."""
    from acorn.tui.actions import load_keymap

    keymap = load_keymap()
    assert (
        "f3" not in keymap.bindings
    ), f"F3 should not be bound; keymap.bindings has: {keymap.bindings.get('f3')!r}"
