"""Phase 5.7: per-chunk widgets, click=preview-only, footer dedup."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from acorn import opener
from acorn.index import build_index
from acorn.tui import AcornApp
from acorn.tui.actions import REGISTRY


@pytest.fixture
def two_pdf_index(fixtures_dir: Path, tmp_index_dir: Path, tmp_path: Path) -> Path:
    """Build an index containing TWO PDFs so we can verify chunk-widget
    rebuild when navigating between files."""
    # The fixture corpus has 1 PDF; clone it under a different name to get
    # two distinct parent_ids.
    import shutil

    extra = tmp_path / "papers" / "second.pdf"
    extra.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(fixtures_dir / "papers" / "test.pdf", extra)
    build_index(roots=[fixtures_dir, tmp_path], index_dir=tmp_index_dir, collection="default")
    return tmp_index_dir


@pytest.fixture
def built_index(fixtures_dir: Path, tmp_index_dir: Path) -> Path:
    build_index(roots=[fixtures_dir], index_dir=tmp_index_dir, collection="default")
    return tmp_index_dir


# ── Click-on-tree should preview, not open ──────────────────────────


@pytest.mark.asyncio
async def test_enter_does_not_open_external_app(
    built_index: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Phase 5.7: pressing Enter on a section node must NOT call the opener."""
    calls: list[Any] = []
    monkeypatch.setattr(opener, "open_smart", lambda **kw: calls.append(kw) or 0)
    app = AcornApp(index_dir=built_index, initial_query="blue penguin sandwich")
    async with app.run_test() as pilot:
        await pilot.pause()
        from textual.widgets import Tree

        tree = app.query_one("#results_pane", Tree)
        first = next(iter(tree.root.children))
        first.expand()
        await pilot.pause()
        tree.focus()
        await pilot.press("down")
        await pilot.press("enter")
        await pilot.pause()
    assert calls == [], f"Enter should not trigger open; got {calls}"


@pytest.mark.asyncio
async def test_o_key_action_open_at_locator(
    built_index: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[Any] = []
    monkeypatch.setattr(opener, "open_smart", lambda **kw: seen.append(kw) or 0)
    app = AcornApp(index_dir=built_index, initial_query="blue penguin sandwich")
    async with app.run_test() as pilot:
        await pilot.pause()
        from textual.widgets import Tree

        tree = app.query_one("#results_pane", Tree)
        first = next(iter(tree.root.children))
        first.expand()
        await pilot.pause()
        tree.focus()
        await pilot.press("down")
        app.action_open_at_locator()
        await pilot.pause()
    assert seen
    assert seen[-1]["kind"] == "pdf"
    assert seen[-1]["page"] == 7


@pytest.mark.asyncio
async def test_capital_o_action_open_default_app(
    built_index: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[Path] = []
    monkeypatch.setattr(opener, "open_default", lambda p: seen.append(p) or 0)
    app = AcornApp(index_dir=built_index, initial_query="blue penguin sandwich")
    async with app.run_test() as pilot:
        await pilot.pause()
        from textual.widgets import Tree

        tree = app.query_one("#results_pane", Tree)
        first = next(iter(tree.root.children))
        first.expand()
        await pilot.pause()
        tree.focus()
        await pilot.press("down")
        app.action_open_default_app()
        await pilot.pause()
    assert seen
    assert str(seen[0]).endswith("test.pdf")


# ── Per-chunk widget rebuild across files ───────────────────────────


@pytest.mark.asyncio
async def test_chunk_widgets_rebuild_when_focus_moves_to_different_file(
    two_pdf_index: Path,
) -> None:
    """The bug user reported: 'doesn't find the correct section at all in
    the second matching document'. Switching between two files must drop
    the previous file's chunk widgets and mount fresh ones for the new
    file, with a focus marker on the right chunk."""
    app = AcornApp(index_dir=two_pdf_index, initial_query="blue penguin sandwich")
    async with app.run_test() as pilot:
        await pilot.pause()
        from textual.widgets import Tree

        tree = app.query_one("#results_pane", Tree)
        children = list(tree.root.children)
        assert len(children) == 2, f"expected 2 PDFs in tree, got {len(children)}"

        # Expand and focus first PDF's first section.
        children[0].expand()
        await pilot.pause()
        tree.focus()
        await pilot.press("down")
        await pilot.pause()
        first_pid = app._preview_parent_id
        first_widgets = dict(app._chunk_widgets)
        assert len(first_widgets) == 12

        # Move to the SECOND PDF in the tree.
        children[1].expand()
        await pilot.pause()
        # Cursor down through remaining children of file-1, then onto file-2.
        for _ in range(20):
            await pilot.press("down")
            await pilot.pause()
            if app._preview_parent_id != first_pid:
                break
        assert app._preview_parent_id is not None
        assert app._preview_parent_id != first_pid, "preview did not rebuild for file 2"
        # Fresh chunks mounted for the new file.
        assert len(app._chunk_widgets) == 12
        # And exactly one chunk marked as focused.
        focused = [w for w in app._chunk_widgets.values() if w.has_class("chunk-section-focused")]
        assert len(focused) == 1


# ── Footer label dedup ──────────────────────────────────────────────


def test_footer_labels_are_unique() -> None:
    """No two visible footer entries share a label — fixes the original
    'two toggles, two opens' duplication."""
    labels = [a.footer_label for a in REGISTRY if a.show_in_footer and a.footer_label]
    assert len(labels) == len(set(labels)), f"duplicate footer labels: {labels}"


def test_every_action_has_footer_label() -> None:
    """Every action that's shown in the footer must have an explicit
    footer_label (no fallback to first-word-of-description heuristic)."""
    for a in REGISTRY:
        if a.show_in_footer:
            assert a.footer_label, f"{a.id} missing footer_label"
