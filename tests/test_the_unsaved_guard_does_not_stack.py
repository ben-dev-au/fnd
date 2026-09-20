"""Pressing `:` twice on a dirty editor stacked a second guard that had
dropped its own Save option.

The second one replaced `Save changes` with `Cannot save yet — the screen
holding it is behind this one`, which describes the screen stack rather than
anything the user did. Esc pops one layer per press and the layers render
identically, so the way back to a dialog that can save is a guess.

`:` is the screen's own footer key (`:  Menu`), and a keyboard user mashes it
when a screen feels stuck.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from textual.widgets import OptionList

from fnd.filters import FilterSpec
from fnd.filters.scan import SourceSample
from fnd.index import build_index
from fnd.tui import FNDApp
from fnd.tui.settings_screen import FilterBrowserScreen, UnsavedChangesScreen
from tests._pilot_wait import wait_until

_SAMPLE = SourceSample(kinds={"md": 3}, tags={"frontmatter": {"no_index": 1, "private": 2}})
_INHERITED = (FilterSpec(exclude_tags={"frontmatter": ("no_index",)}), True, True)
_MINE = FilterSpec(kinds=("md", "txt"), exclude_tags={"frontmatter": ("no_index", "private")})


@pytest.fixture
def built_index(tmp_path: Path, tmp_index_dir: Path) -> Path:
    root = tmp_path / "corpus"
    root.mkdir()
    (root / "a.md").write_text("# A\n\nrisotto.\n", encoding="utf-8")
    build_index(roots=[root], index_dir=tmp_index_dir, collection="c")
    return tmp_index_dir


def _guards(app: FNDApp) -> list[UnsavedChangesScreen]:
    return [s for s in app.screen_stack if isinstance(s, UnsavedChangesScreen)]


def _offers_save(screen: UnsavedChangesScreen) -> bool:
    return any(o.id == "save" for o in screen.query_one("#confirm_list", OptionList).options)


@pytest.mark.asyncio
async def test_pressing_the_menu_key_twice_leaves_one_guard(built_index: Path) -> None:
    app = FNDApp(index_dir=built_index)
    async with app.run_test(size=(110, 34)) as pilot:
        screen = FilterBrowserScreen(
            title="Index filters",
            spec=_MINE,
            gitignore=True,
            fndignore=True,
            inherited=_INHERITED,
            sample_provider=lambda _spec: _SAMPLE,
            on_save=lambda *_a: None,
        )
        app.push_screen(screen)
        # The rows, not the screen: `action_clear_all` rebuilds the tree, and
        # a screen that exists a frame before it composes has none.
        await wait_until(
            pilot,
            lambda: app.screen is screen and bool(screen.query("#filter_tree")),
            timeout=20.0,
            message="the filter tree never composed",
        )
        screen.action_clear_all()
        await pilot.pause()
        assert screen._dirty(), "the state under test is a dirty editor"

        app.action_open_command_palette()
        await wait_until(
            pilot,
            lambda: bool(_guards(app)) and bool(_guards(app)[0].query("#confirm_list")),
            timeout=20.0,
            message="the guard never composed its options",
        )
        first = _guards(app)
        assert len(first) == 1, "the baseline is one guard"
        assert _offers_save(first[0]), "the baseline guard offers to save"

        app.action_open_command_palette()
        await wait_until(
            pilot,
            lambda: bool(_guards(app)) and bool(_guards(app)[-1].query("#confirm_list")),
            timeout=20.0,
            message="no guard is on screen at all after the second press",
        )
        after = _guards(app)
        same = after[0] is first[0]
        offers = _offers_save(after[-1])

    assert len(after) == 1, f"a second guard was stacked: {len(after)}"
    assert same, "the standing guard was replaced rather than left alone"
    assert offers, "the standing guard lost the option that saves the work"


@pytest.mark.asyncio
async def test_quitting_twice_leaves_one_guard(built_index: Path) -> None:
    """`q` and ctrl+c are the third route that asks the question, and it kept
    stacking after the other two stopped."""
    app = FNDApp(index_dir=built_index)
    async with app.run_test(size=(110, 34)) as pilot:
        screen = FilterBrowserScreen(
            title="Index filters",
            spec=_MINE,
            gitignore=True,
            fndignore=True,
            inherited=_INHERITED,
            sample_provider=lambda _spec: _SAMPLE,
            on_save=lambda *_a: None,
        )
        app.push_screen(screen)
        await wait_until(
            pilot,
            lambda: app.screen is screen and bool(screen.query("#filter_tree")),
            timeout=20.0,
            message="the filter tree never composed",
        )
        screen.action_clear_all()
        await pilot.pause()

        app.action_quit()
        await wait_until(
            pilot,
            lambda: bool(_guards(app)) and bool(_guards(app)[-1].query("#confirm_list")),
            timeout=20.0,
            message="quit never asked",
        )
        first = _guards(app)

        app.action_quit()
        await wait_until(
            pilot,
            lambda: bool(_guards(app)) and bool(_guards(app)[-1].query("#confirm_list")),
            timeout=20.0,
            message="no guard on screen after the second quit",
        )
        after = _guards(app)
        same = after[0] is first[0]
        offers = _offers_save(after[-1])

    assert len(after) == 1, f"a second guard was stacked: {len(after)}"
    assert same, "the standing guard was replaced rather than left alone"
    assert offers, "the standing guard lost the option that saves the work"
