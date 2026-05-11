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


def test_detail_strip_renders_description_and_metadata() -> None:
    """Spec: Visual system › Detail strip — 2 lines, description then
    metadata in $text-muted."""
    from acorn.tui.widgets.detail_strip import DetailStrip

    strip = DetailStrip()
    strip._description = "Result limit (1–1000) — max results returned per query."
    strip._metadata = "Stored in defaults.result_limit · Applies on next search"
    rendered = strip._render_lines()
    assert len(rendered) == 2
    assert "Result limit" in str(rendered[0])
    assert "Stored in defaults.result_limit" in str(rendered[1])


def test_row_with_key_renders_bracketed_accent() -> None:
    """Spec: Visual system › Key style — bracketed `[o]` accent."""
    from acorn.tui.menu import KIND_ACTION, MenuItem
    from acorn.tui.settings_screen import _render_row

    item = MenuItem(
        id="k.test",
        label="Open at locator",
        kind=KIND_ACTION,
        key="o",
        action_id="open_at_locator",
    )
    rendered = _render_row(item, app=None, width=80)
    text_str = str(rendered)
    assert "[o]" in text_str, f"expected '[o]' in rendered row; got: {text_str!r}"
    assert "▶" not in text_str
