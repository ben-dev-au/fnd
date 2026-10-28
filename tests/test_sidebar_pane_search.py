"""The search field atop the Collections and Filters panes narrows their rows."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from rich.style import Style
from textual.widgets import Tree

from fnd.config import Config, load
from fnd.state import UiState, save
from fnd.tui import FNDApp
from fnd.tui.widgets.pane_search import PaneSearch

_COLLECTIONS = ("alpha", "beta", "gamma", "zebra", "zephyr")


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    lines: list[str] = []
    for name in _COLLECTIONS:
        for src in ("inbox", "archive") if name == "zephyr" else ("main",):
            d = tmp_path / "corpus" / name / src
            d.mkdir(parents=True)
            lines += [f"[[collections.{name}.sources]]", f'path = "{d.as_posix()}"', ""]
    p = tmp_path / "config.toml"
    p.write_text("\n".join(lines), encoding="utf-8")
    return load(p)


async def _settle(pilot: Any, n: int = 4) -> None:
    for _ in range(n):
        await pilot.pause()


async def _press_from_results(app: FNDApp, pilot: Any, *keys: str) -> None:
    """Pane keys are letters, so they need focus off the query bar first."""
    app.query_one("#results_pane", Tree).focus()
    await _settle(pilot)
    await pilot.press(*keys)
    await _settle(pilot)


def _rows(tree: Tree[Any]) -> list[str]:
    return [str((line.node.data or {}).get("text", "")) for line in tree._tree_lines]


def _text(node: Any) -> str:
    return str((node.data or {}).get("text", ""))


def _collections(app: FNDApp) -> Tree[Any]:
    return app.query_one("#collections_panel_tree", Tree)


def _filters(app: FNDApp) -> Tree[Any]:
    return app.query_one("#filters_panel_tree", Tree)


async def _search(app: FNDApp, pilot: Any, pane: str, text: str) -> PaneSearch:
    field = app.query_one(f"#{pane}_search", PaneSearch)
    field.focus()
    await _settle(pilot)
    for ch in text:
        await pilot.press(ch)
    await _settle(pilot)
    return field


@pytest.mark.asyncio
async def test_opening_a_pane_still_lands_on_its_rows(cfg: Config, tmp_index_dir: Path) -> None:
    """`c` and `f` focus the tree, never the field."""
    app = FNDApp(index_dir=tmp_index_dir, config=cfg)
    async with app.run_test(size=(120, 50)) as pilot:
        await _settle(pilot)
        await _press_from_results(app, pilot, "c")
        assert app.focused is _collections(app)
        await pilot.press("f")
        await _settle(pilot)
        assert app.focused is _filters(app)


@pytest.mark.asyncio
@pytest.mark.parametrize("pane", ["collections", "filters"])
async def test_up_from_the_top_row_reaches_the_field(
    cfg: Config, tmp_index_dir: Path, pane: str
) -> None:
    """Up on the tree's first row focuses that pane's search field."""
    app = FNDApp(index_dir=tmp_index_dir, config=cfg)
    async with app.run_test(size=(120, 50)) as pilot:
        await _settle(pilot)
        await _press_from_results(app, pilot, pane[0])
        tree = app.focused
        assert isinstance(tree, Tree)
        tree.action_cursor_first()  # type: ignore[attr-defined]
        await _settle(pilot)
        await pilot.press("up")
        await _settle(pilot)
        assert app.focused is app.query_one(f"#{pane}_search", PaneSearch)


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["ctrl+slash", "ctrl+underscore"])
async def test_ctrl_slash_reaches_the_field_from_any_row(
    cfg: Config, tmp_index_dir: Path, key: str
) -> None:
    """Both spellings a terminal sends for Ctrl+/ focus the field."""
    app = FNDApp(index_dir=tmp_index_dir, config=cfg)
    async with app.run_test(size=(120, 50)) as pilot:
        await _settle(pilot)
        await _press_from_results(app, pilot, "c", "down", "down")
        await pilot.press(key)
        await _settle(pilot)
        assert app.focused is app.query_one("#collections_search", PaneSearch)


@pytest.mark.asyncio
async def test_typing_keeps_matches_and_opens_their_parents(
    cfg: Config, tmp_index_dir: Path
) -> None:
    """A source match shows under its collection, opened, without persisting the open."""
    app = FNDApp(index_dir=tmp_index_dir, config=cfg)
    async with app.run_test(size=(120, 50)) as pilot:
        await _settle(pilot)
        await _search(app, pilot, "collections", "INB")
        assert _rows(_collections(app)) == ["zephyr", "inbox"]
        assert app._scope.expanded_collections == set()


@pytest.mark.asyncio
async def test_a_matching_parent_keeps_its_children(cfg: Config, tmp_index_dir: Path) -> None:
    """A collection that matches stays whole: open it and every source is there."""
    app = FNDApp(index_dir=tmp_index_dir, config=cfg)
    async with app.run_test(size=(120, 50)) as pilot:
        await _settle(pilot)
        await _search(app, pilot, "collections", "zeph")
        tree = _collections(app)
        assert _rows(tree) == ["zephyr"]
        assert [_text(c) for c in tree.root.children[0].children] == ["inbox", "archive"]


@pytest.mark.asyncio
async def test_shortcut_letters_type_into_the_field(cfg: Config, tmp_index_dir: Path) -> None:
    """`q`, `f` and `c` are text in the field, not quit or pane jumps."""
    app = FNDApp(index_dir=tmp_index_dir, config=cfg)
    async with app.run_test(size=(120, 50)) as pilot:
        await _settle(pilot)
        field = await _search(app, pilot, "collections", "qfc")
        assert field.value == "qfc"
        assert app.focused is field
        assert app.is_running


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["enter", "down"])
async def test_enter_and_down_move_to_the_first_match(
    cfg: Config, tmp_index_dir: Path, key: str
) -> None:
    """Leaving the field downwards lands the cursor on the first visible row."""
    app = FNDApp(index_dir=tmp_index_dir, config=cfg)
    async with app.run_test(size=(120, 50)) as pilot:
        await _settle(pilot)
        await _search(app, pilot, "collections", "ze")
        await pilot.press(key)
        await _settle(pilot)
        tree = _collections(app)
        assert app.focused is tree
        assert tree.cursor_node is not None
        assert _text(tree.cursor_node) == "zebra"


@pytest.mark.asyncio
async def test_a_rebuild_keeps_the_filter(cfg: Config, tmp_index_dir: Path) -> None:
    """Toggling a filtered row and a full rebuild both leave the rows narrowed."""
    app = FNDApp(index_dir=tmp_index_dir, config=cfg)
    async with app.run_test(size=(120, 50)) as pilot:
        await _settle(pilot)
        await _search(app, pilot, "collections", "zeb")
        before = set(app._scope.collections)
        await pilot.press("enter", "enter")
        await _settle(pilot)
        assert set(app._scope.collections) ^ before == {"zebra"}
        app._scope.refresh_collections_panel()
        await _settle(pilot)
        assert _rows(_collections(app)) == ["zebra"]


@pytest.mark.asyncio
async def test_escape_clears_then_leaves(cfg: Config, tmp_index_dir: Path) -> None:
    """Esc empties the field and restores every row; Esc again goes to Results."""
    app = FNDApp(index_dir=tmp_index_dir, config=cfg)
    async with app.run_test(size=(120, 50)) as pilot:
        await _settle(pilot)
        field = await _search(app, pilot, "collections", "zeb")
        await pilot.press("escape")
        await _settle(pilot)
        assert field.value == ""
        assert app.focused is field
        assert _rows(_collections(app)) == list(_COLLECTIONS)
        await pilot.press("escape")
        await _settle(pilot)
        assert app.focused is app.query_one("#results_pane", Tree)


@pytest.mark.asyncio
async def test_clearing_restores_the_cursor_and_the_open_state(
    cfg: Config, tmp_index_dir: Path
) -> None:
    """The cursor returns to the row last used, and auto-opened parents close again."""
    app = FNDApp(index_dir=tmp_index_dir, config=cfg)
    async with app.run_test(size=(120, 50)) as pilot:
        await _settle(pilot)
        await _press_from_results(app, pilot, "c", "home", "down", "down")
        tree = _collections(app)
        assert tree.cursor_node is not None
        assert _text(tree.cursor_node) == "gamma"
        await pilot.press("ctrl+underscore")
        for ch in "inb":
            await pilot.press(ch)
        await pilot.press("escape")
        await _settle(pilot)
        assert tree.cursor_node is not None
        assert _text(tree.cursor_node) == "gamma"
        zephyr = tree.root.children[-1]
        assert _text(zephyr) == "zephyr"
        assert not zephyr.is_expanded


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["", "zeb"])
async def test_left_at_the_start_collapses_the_pane(
    cfg: Config, tmp_index_dir: Path, text: str
) -> None:
    """Left collapses the pane only once the caret is at the far left, text or not."""
    app = FNDApp(index_dir=tmp_index_dir, config=cfg)
    async with app.run_test(size=(120, 50)) as pilot:
        await _settle(pilot)
        await _search(app, pilot, "collections", text)
        pane = app.query_one("#collections_pane")
        if text:
            await pilot.press("left")
            await _settle(pilot)
            assert "collapsed" not in pane.classes
            await pilot.press("home")
        await pilot.press("left")
        await _settle(pilot)
        assert "collapsed" in pane.classes
        assert "collections_pane" in app._scope.collapsed_panels


@pytest.mark.asyncio
async def test_filters_search_reaches_nested_values(cfg: Config, tmp_index_dir: Path) -> None:
    """A value under a closed branch shows with its branch opened, not persisted."""
    app = FNDApp(index_dir=tmp_index_dir, config=cfg)
    async with app.run_test(size=(120, 50)) as pilot:
        await _settle(pilot)
        before = set(app._scope.expanded_filter_branches)
        await _search(app, pilot, "filters", "week")
        assert _rows(_filters(app)) == ["Modified", "week", "Created", "week"]
        assert app._scope.expanded_filter_branches == before
        app.query_one("#filters_search", PaneSearch).value = ""
        await _settle(pilot)
        assert app._scope.expanded_filter_branches == before


@pytest.mark.asyncio
async def test_the_matched_text_is_bold(cfg: Config, tmp_index_dir: Path) -> None:
    """The typed text is emboldened inside a matching row's label."""
    app = FNDApp(index_dir=tmp_index_dir, config=cfg)
    async with app.run_test(size=(120, 50)) as pilot:
        await _settle(pilot)
        await _search(app, pilot, "collections", "BRA")
        tree = _collections(app)
        node = tree.root.children[0]
        rendered = tree.render_label(node, Style(), Style())
        at = rendered.plain.find("bra")
        assert at >= 0
        assert any(
            s.start <= at and s.end >= at + 3 and Style.parse(str(s.style)).bold
            for s in rendered.spans
        ), rendered.spans


@pytest.mark.asyncio
async def test_the_pane_has_room_for_the_field_and_every_row(
    cfg: Config, tmp_index_dir: Path
) -> None:
    """With height to spare, the field's row does not push a row out of view."""
    app = FNDApp(index_dir=tmp_index_dir, config=cfg)
    async with app.run_test(size=(120, 60)) as pilot:
        await _settle(pilot, 8)
        tree = _collections(app)
        assert tree.size.height >= tree.virtual_size.height > 0


@pytest.mark.asyncio
async def test_the_field_sits_above_the_clear_bar(cfg: Config, tmp_index_dir: Path) -> None:
    """With a filter active, field, clear bar and tree each get their own row, in that order."""
    app = FNDApp(index_dir=tmp_index_dir, config=cfg)
    async with app.run_test(size=(120, 50)) as pilot:
        await _settle(pilot)
        app._scope.filter_date = "week"
        app._scope.refresh_filters_panel()
        await _settle(pilot, 6)
        field = app.query_one("#filters_search").region
        bar = app.query_one("#clear_filters_bar")
        assert bar.visible
        assert field.y < bar.region.y < _filters(app).region.y


@pytest.mark.asyncio
async def test_an_old_collapsed_collections_id_still_collapses(
    cfg: Config, tmp_index_dir: Path
) -> None:
    """A state file naming the bare tree collapses the new pane."""
    save(UiState(saved=True, collapsed_panels=["collections_panel_tree"]))
    app = FNDApp(index_dir=tmp_index_dir, config=cfg)
    async with app.run_test(size=(120, 50)) as pilot:
        await _settle(pilot)
        assert "collapsed" in app.query_one("#collections_pane").classes


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["tab", "shift+tab"])
async def test_tab_steps_past_the_search_fields(cfg: Config, tmp_index_dir: Path, key: str) -> None:
    """Tab and Shift+Tab land on a pane's rows, never its search field."""
    app = FNDApp(index_dir=tmp_index_dir, config=cfg)
    async with app.run_test(size=(120, 50)) as pilot:
        await _settle(pilot)
        app.query_one("#results_pane", Tree).focus()
        await _settle(pilot)
        seen: list[str] = []
        for _ in range(8):
            await pilot.press(key)
            await _settle(pilot, 2)
            seen.append(str(getattr(app.focused, "id", None)))
        assert "collections_panel_tree" in seen
        assert "filters_panel_tree" in seen
        assert not any(s.endswith("_search") for s in seen), seen


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("pane", "text", "persisted"),
    [
        ("collections", "inb", "expanded_collections"),
        ("filters", "week", "expanded_filter_branches"),
    ],
)
async def test_branches_a_search_opened_are_not_saved_when_it_clears_at_once(
    cfg: Config, tmp_index_dir: Path, pane: str, text: str, persisted: str
) -> None:
    """Clearing before the search's open events are handled still saves none of them."""
    app = FNDApp(index_dir=tmp_index_dir, config=cfg)
    async with app.run_test(size=(120, 50)) as pilot:
        await _settle(pilot)
        before = set(getattr(app._scope, persisted))
        field = app.query_one(f"#{pane}_search", PaneSearch)
        field.value = text
        field.value = ""
        await _settle(pilot, 8)
        assert getattr(app._scope, persisted) == before


@pytest.mark.asyncio
async def test_a_branch_opened_during_a_search_survives_a_toggle(
    cfg: Config, tmp_index_dir: Path
) -> None:
    """Toggling inside a branch the user opened mid-search leaves it open, unsaved."""
    app = FNDApp(index_dir=tmp_index_dir, config=cfg)
    async with app.run_test(size=(120, 50)) as pilot:
        await _settle(pilot)
        await _search(app, pilot, "filters", "modif")
        await pilot.press("enter", "right")
        await _settle(pilot)
        tree = _filters(app)
        assert _rows(tree) == ["Modified", "today", "week", "month", "year"]
        await pilot.press("enter")
        await _settle(pilot)
        assert app._scope.filter_date == "today"
        assert _rows(tree) == ["Modified", "today", "week", "month", "year"]
        assert tree.cursor_node is not None
        assert _text(tree.cursor_node) == "today"
        app.query_one("#filters_search", PaneSearch).value = ""
        await _settle(pilot)
        assert "date" not in app._scope.expanded_filter_branches


@pytest.mark.asyncio
async def test_a_branch_closed_during_a_search_stays_closed(
    cfg: Config, tmp_index_dir: Path
) -> None:
    """Closing a saved-open branch mid-search holds until the search clears, unsaved."""
    app = FNDApp(index_dir=tmp_index_dir, config=cfg)
    async with app.run_test(size=(120, 50)) as pilot:
        await _settle(pilot)
        next(n for n in _filters(app).root.children if _text(n) == "Modified").expand()
        await _settle(pilot)
        assert "date" in app._scope.expanded_filter_branches
        await _search(app, pilot, "filters", "modi")
        await pilot.press("enter", "left")
        await _settle(pilot)
        tree = _filters(app)
        assert _rows(tree) == ["Modified"]
        field = await _search(app, pilot, "filters", "")
        field.value = "modif"
        await _settle(pilot)
        assert _rows(tree) == ["Modified"]
        field.value = ""
        await _settle(pilot)
        assert "date" in app._scope.expanded_filter_branches


@pytest.mark.asyncio
async def test_clearing_finds_the_row_after_the_tree_has_shrunk(
    cfg: Config, tmp_index_dir: Path
) -> None:
    """A rebuild with fewer rows than an earlier one does not misplace the restored cursor."""
    app = FNDApp(index_dir=tmp_index_dir, config=cfg)
    async with app.run_test(size=(120, 50)) as pilot:
        await _settle(pilot)
        app._scope.expanded_collections.add("zephyr")
        app._scope.refresh_collections_panel()
        await _settle(pilot)
        tree = _collections(app)
        tree.cursor_line = _rows(tree).index("archive")
        assert app._config is not None
        del app._config.collections["alpha"]
        app._scope.refresh_collections_panel()
        await _settle(pilot)
        await _search(app, pilot, "collections", "bet")
        await pilot.press("escape")
        await _settle(pilot)
        assert tree.cursor_node is not None
        assert _text(tree.cursor_node) == "archive"


@pytest.mark.asyncio
async def test_ctrl_slash_leaves_a_folded_results_pane_alone(
    cfg: Config, tmp_index_dir: Path
) -> None:
    """Outside the scope panes Ctrl+/ does nothing, rather than reopening the pane."""
    app = FNDApp(index_dir=tmp_index_dir, config=cfg)
    async with app.run_test(size=(120, 50)) as pilot:
        await _settle(pilot)
        results = app.query_one("#results_pane", Tree)
        results.focus()
        app._collapse_panel_frame(results)
        await _settle(pilot)
        await pilot.press("ctrl+underscore")
        await _settle(pilot)
        assert "collapsed" in results.classes
        assert app.focused is results


@pytest.mark.asyncio
async def test_a_blank_search_still_saves_the_users_folds(cfg: Config, tmp_index_dir: Path) -> None:
    """Spaces alone show every row and are no search, so opening a branch is saved."""
    app = FNDApp(index_dir=tmp_index_dir, config=cfg)
    async with app.run_test(size=(120, 50)) as pilot:
        await _settle(pilot)
        await _search(app, pilot, "collections", "  ")
        assert _rows(_collections(app)) == list(_COLLECTIONS)
        _collections(app).root.children[-1].expand()
        await _settle(pilot)
        assert app._scope.expanded_collections == {"zephyr"}


@pytest.mark.asyncio
async def test_typing_reuses_the_tag_catalogue(
    cfg: Config, tmp_index_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A keystroke in the Filters search never re-runs the tag facet query."""
    app = FNDApp(index_dir=tmp_index_dir, config=cfg)
    async with app.run_test(size=(120, 50)) as pilot:
        await _settle(pilot)
        calls: list[int] = []
        monkeypatch.setattr(app._scope, "tag_catalogue_for_scope", lambda: calls.append(1) or {})
        await _search(app, pilot, "filters", "wee")
        assert calls == []
