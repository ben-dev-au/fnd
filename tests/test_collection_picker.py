"""Phase 5.5c: collection picker UI."""

from __future__ import annotations

from pathlib import Path

import pytest

from acorn.index import build_index
from acorn.tui import AcornApp


def _write_md(p: Path, body: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")


@pytest.fixture
def two_collection_index(tmp_path: Path, tmp_index_dir: Path) -> Path:
    """Build a tiny index containing two collections so the picker has
    something to show."""
    a_root = tmp_path / "papers"
    b_root = tmp_path / "notes"
    _write_md(a_root / "a.md", "# A\nshared anchor word: glimmer\n")
    _write_md(b_root / "b.md", "# B\nshared anchor word: glimmer\n")
    build_index(roots=[a_root], index_dir=tmp_index_dir, collection="papers")
    build_index(roots=[b_root], index_dir=tmp_index_dir, collection="notes")
    return tmp_index_dir


@pytest.fixture(autouse=True)
def _stub_config(  # pyright: ignore[reportUnusedFunction]
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Point the picker's config loader at a temp TOML defining both
    collections so it has something to enumerate."""
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        "[collections.papers]\nroots=[]\n\n[collections.notes]\nroots=[]\n",
        encoding="utf-8",
    )
    import acorn.config as ac

    monkeypatch.setattr(ac, "default_config_path", lambda: cfg_path)


@pytest.mark.asyncio
async def test_picker_opens_with_known_collections(two_collection_index: Path) -> None:
    app = AcornApp(index_dir=two_collection_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_open_collection_picker()
        await pilot.pause()
        from textual.widgets import SelectionList

        sel = app.query_one("#collection_selection", SelectionList)
        # Both collections should be options.
        labels = {opt.value for opt in sel._options}  # type: ignore[attr-defined]
        assert {"papers", "notes"}.issubset(labels)


@pytest.mark.asyncio
async def test_picker_toggle_updates_active_scope(two_collection_index: Path) -> None:
    app = AcornApp(index_dir=two_collection_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_open_collection_picker()
        await pilot.pause()
        from textual.widgets import SelectionList

        sel = app.query_one("#collection_selection", SelectionList)
        sel.select(sel.get_option_at_index(0))
        await pilot.pause()
        # _collections should now reflect the selected option.
        assert len(app._collections) == 1


@pytest.mark.asyncio
async def test_picker_multi_select_scopes_query(two_collection_index: Path) -> None:
    """Selecting both collections → search must be scoped to both, not all."""
    app = AcornApp(index_dir=two_collection_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_open_collection_picker()
        await pilot.pause()
        from textual.widgets import SelectionList

        sel = app.query_one("#collection_selection", SelectionList)
        sel.select(sel.get_option_at_index(0))
        sel.select(sel.get_option_at_index(1))
        await pilot.pause()
        assert len(app._collections) == 2

        # Drive the search directly — picker focus / Enter routing is exercised
        # in the dedicated submit-flow tests.
        app._run_query("glimmer")
        await pilot.pause()
        # Both collections include the anchor — both should appear in groups.
        parent_ids = {g.parent_id for g in app._groups}
        assert len(parent_ids) == 2, f"expected hits from both collections, got {parent_ids}"


@pytest.mark.asyncio
async def test_single_collection_scope_does_not_use_dsl_prefix(
    two_collection_index: Path,
) -> None:
    """With one collection selected, the searcher's `collection` arg is used
    directly (DSL prefix not needed). Hits must come from only that collection."""
    app = AcornApp(index_dir=two_collection_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._collections = ["papers"]
        app._run_query("glimmer")
        await pilot.pause()
        assert len(app._groups) == 1
        # The hit must be from papers/a.md.
        assert app._groups[0].path.endswith("papers/a.md")


@pytest.mark.asyncio
async def test_picker_enter_dismisses_without_untoggling(two_collection_index: Path) -> None:
    """Enter must close the picker, leaving the user's Space-toggled
    selections intact. The default ``SelectionList`` treats Enter the
    same as Space (toggle), which silently undoes everything the user
    just selected — this regression test pins down the corrected
    behaviour."""
    app = AcornApp(index_dir=two_collection_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_open_collection_picker()
        await pilot.pause()
        from textual.widgets import SelectionList

        sel = app.query_one("#collection_selection", SelectionList)
        sel.focus()
        await pilot.pause()
        # Toggle the first option with Space.
        await pilot.press("space")
        await pilot.pause()
        assert len(app._collections) == 1, "Space should toggle one option on"

        # Press Enter — should close the picker and PRESERVE the toggle.
        await pilot.press("enter")
        await pilot.pause()
        assert len(app._collections) == 1, "Enter must apply (dismiss), not untoggle the selection"
        assert not app.query("#collection_picker"), "Enter must close the picker"


@pytest.mark.asyncio
async def test_picker_toggle_closes_when_already_open(two_collection_index: Path) -> None:
    app = AcornApp(index_dir=two_collection_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_open_collection_picker()
        await pilot.pause()
        assert app.query("#collection_picker")
        app.action_open_collection_picker()
        await pilot.pause()
        assert not app.query("#collection_picker")
