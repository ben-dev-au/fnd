"""Per-chunk widgets, click=preview-only, footer dedup."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from fnd import opener
from fnd.index import build_index
from fnd.tui import FNDApp
from fnd.tui.actions import REGISTRY
from tests._pilot_wait import wait_until


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
    # Index the clone's dir, not all of tmp_path: the isolation fixtures drop
    # fnd's own caches (pdf-structure-cache/*.json) under tmp_path, which would
    # otherwise be indexed now that .json is a supported type.
    build_index(roots=[fixtures_dir, extra.parent], index_dir=tmp_index_dir, collection="default")
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
    """Pressing Enter on a section node must NOT call the opener."""
    calls: list[Any] = []
    monkeypatch.setattr(opener, "open_smart", lambda **kw: calls.append(kw) or 0)
    app = FNDApp(index_dir=built_index, initial_query="blue penguin sandwich")
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
    app = FNDApp(index_dir=built_index, initial_query="blue penguin sandwich")
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
    app = FNDApp(index_dir=built_index, initial_query="blue penguin sandwich")
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
    """Contract for the same bug ('doesn't find the correct
    section in the second document'): switching between two PDFs swaps
    the active LineBufferPreview to the second file's widget, keyed by
    its parent_id, and the new FileView covers all 12 pages."""
    app = FNDApp(index_dir=two_pdf_index, initial_query="blue penguin sandwich")
    async with app.run_test() as pilot:
        await pilot.pause()
        from textual.widgets import Tree

        tree = app.query_one("#results_pane", Tree)
        children = list(tree.root.children)
        assert len(children) == 2, f"expected 2 PDFs in tree, got {len(children)}"

        children[0].expand()
        await pilot.pause()
        tree.focus()
        first_leaf_zero = children[0].children[0]
        tree.move_cursor(first_leaf_zero)
        # A cursor move dispatches a debounced load, a decode worker and a
        # mount; one pause covers that only while the machine is idle.
        await wait_until(
            pilot,
            lambda: (
                app._preview.parent_id is not None
                and app._flat.active_buffer is not None
                and app._flat.active_buffer.file_view is not None
            ),
            timeout=30.0,
            message="the first PDF never became the active preview",
        )
        first_pid = app._preview.parent_id
        first_buf = app._flat.active_buffer
        assert first_buf is not None
        first_fv = first_buf.file_view
        assert first_fv is not None
        assert len(first_fv.chunk_to_range) == 12

        # Jump to the second PDF.
        children[1].expand()
        await pilot.pause()
        second_leaf_zero = children[1].children[0]
        tree.move_cursor(second_leaf_zero)
        await wait_until(
            pilot,
            lambda: app._preview.parent_id not in (None, first_pid),
            timeout=30.0,
            message="preview did not rebuild for file 2",
        )
        assert app._preview.parent_id is not None
        assert app._preview.parent_id != first_pid, "preview did not rebuild for file 2"
        # Stage 1c: a single shared LineBufferPreview is reused across files —
        # what swaps is the installed RenderedDocument (and therefore file_view).
        second_buf = app._flat.active_buffer
        assert second_buf is not None
        second_fv = second_buf.file_view
        assert second_fv is not None
        assert second_fv is not first_fv, "file_view should swap when file changes"
        assert len(second_fv.chunk_to_range) == 12


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
