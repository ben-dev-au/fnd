"""UX-B — scope persistence + section collapse-to-header."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from textual.widgets import Tree

from fnd.config import Config, load
from fnd.index import build_index
from fnd.tui import FNDApp


@pytest.fixture
def cfg_two(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        textwrap.dedent("""
            [[collections.papers.sources]]
            path = "/tmp/papers"
            [[collections.notes.sources]]
            path = "/tmp/notes"
        """),
        encoding="utf-8",
    )
    monkeypatch.setattr("fnd.config.default_config_path", lambda: cfg_path)
    return load(cfg_path)


@pytest.fixture
def two_index(tmp_path: Path, tmp_index_dir: Path) -> Path:
    a = tmp_path / "papers"
    b = tmp_path / "notes"
    a.mkdir()
    b.mkdir()
    (a / "x.md").write_text("# x\nshared anchor: glimmer", encoding="utf-8")
    (b / "y.md").write_text("# y\nshared anchor: glimmer", encoding="utf-8")
    build_index(roots=[a], index_dir=tmp_index_dir, collection="papers")
    build_index(roots=[b], index_dir=tmp_index_dir, collection="notes")
    return tmp_index_dir


@pytest.fixture
def state_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the state-file lookup at a temp path so this test
    doesn't trample the user's real ``scope.toml``.

    Seeded with an empty saved scope: a profile that has never saved one
    starts from ``defaults.collection`` (all collections), and these
    tests are about what a toggle *writes*, so they need a known
    nothing-selected starting point."""
    from fnd.state import UiState, save

    p = tmp_path / "state" / "scope.toml"
    monkeypatch.setattr("fnd.state._state_path", lambda: p)
    save(UiState(), path=p)
    return p


@pytest.mark.asyncio
async def test_scope_toggle_persists_to_disk(
    cfg_two: Config, two_index: Path, state_path: Path
) -> None:
    """Toggling a collection in the panel writes the active list to
    ``scope.toml`` so the next launch starts in the same scope."""
    app = FNDApp(index_dir=two_index, config=cfg_two)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app._scope.collections == []
        tree = app.query_one("#collections_panel_tree", Tree)
        tree.focus()
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert len(app._scope.collections) == 1

    # Re-load state from disk: should reflect the toggle.
    from fnd.state import load as load_state

    saved = load_state(state_path)
    assert len(saved.collections) == 1


@pytest.mark.asyncio
async def test_app_restores_persisted_scope_on_launch(
    cfg_two: Config, two_index: Path, state_path: Path
) -> None:
    """Pre-seed scope.toml; a fresh FNDApp (no --collection) should
    boot with that scope active."""
    from fnd.state import UiState, save

    state_path.parent.mkdir(parents=True, exist_ok=True)
    save(UiState(collections=["papers"]), state_path)

    app = FNDApp(index_dir=two_index, config=cfg_two)  # no collection arg
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app._scope.collections == ["papers"]


@pytest.mark.asyncio
async def test_left_at_collapsed_root_collapses_panel(
    cfg_two: Config, two_index: Path, state_path: Path
) -> None:
    """Left arrow on a top-level collapsed node — with no further parent
    to walk up to — collapses the whole panel to its header strip
    (CSS ``.collapsed`` class)."""
    app = FNDApp(index_dir=two_index, config=cfg_two)
    async with app.run_test() as pilot:
        await pilot.pause()
        tree = app.query_one("#collections_panel_tree", Tree)
        tree.focus()
        await pilot.pause()
        await pilot.press("down")  # cursor on first collection (collapsed)
        await pilot.pause()
        # First left: cursor's on a top-level collapsed node, no parent
        # → panel collapses.
        await pilot.press("left")
        await pilot.pause()
        assert "collapsed" in tree.classes


@pytest.mark.asyncio
async def test_right_re_expands_collapsed_panel(
    cfg_two: Config, two_index: Path, state_path: Path
) -> None:
    """Once the panel's collapsed-to-header, Right re-expands it."""
    app = FNDApp(index_dir=two_index, config=cfg_two)
    async with app.run_test() as pilot:
        await pilot.pause()
        tree = app.query_one("#collections_panel_tree", Tree)
        tree.add_class("collapsed")  # pre-seed the collapsed state
        tree.focus()
        await pilot.pause()
        await pilot.press("right")
        await pilot.pause()
        assert "collapsed" not in tree.classes
