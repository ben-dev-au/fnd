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
