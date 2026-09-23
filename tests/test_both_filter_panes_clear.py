"""Both filter panes carry the same row, doing the act each pane is for.

The sidebar had a focusable `✕ Clear N filters` row; the settings pane cleared
on a key and showed nothing that said so. A key named in a footer is not an
affordance: nothing on the screen offers it, and Up from the top row reached
nothing.

The two acts are not the same. Search filters are ephemeral and clearing them
costs nothing, so that row counts what it removes. Index filters are config:
the row restores what a source inherits, and a set that inherits from nothing
has nothing to return to.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fnd.filters import FilterSpec
from fnd.filters.scan import SourceSample
from fnd.tui import FNDApp
from fnd.tui.settings_screen import FilterBrowserScreen
from fnd.tui.widgets.clear_bar import RETURN_TO_DEFAULTS, ClearFiltersBar, clear_label
from fnd.tui.widgets.toggle_tree import ToggleTree

_SAMPLE = SourceSample(kinds={"md": 3}, tags={"frontmatter": {"no_index": 1, "keep": 2}})
_DEFAULTS = (FilterSpec(exclude_tags={"frontmatter": ("no_index",)}), True, True)


def _painted(app: FNDApp) -> str:
    return "\n".join(
        "".join(s.text for s in strip) for strip in app.screen._compositor.render_strips()
    )


async def _browser(
    app: FNDApp,
    pilot: object,
    spec: FilterSpec,
    inherited: tuple[FilterSpec, bool, bool] | None = None,
) -> FilterBrowserScreen:
    screen = FilterBrowserScreen(
        title="Index filters",
        spec=spec,
        gitignore=True,
        fndignore=True,
        inherited=inherited,
        sample_provider=lambda _spec: _SAMPLE,
        on_save=lambda *_a: None,
    )
    app.push_screen(screen)
    for _ in range(25):
        await pilot.pause()  # type: ignore[attr-defined]
    return screen


def test_the_two_panes_name_two_different_acts() -> None:
    """Same widget, deliberately different words: one clears a search, the
    other returns a source to the defaults it departed from."""
    assert "Clear" in clear_label(1)
    assert "Return to default" in RETURN_TO_DEFAULTS
    assert "Clear " not in RETURN_TO_DEFAULTS


def test_the_search_row_counts_what_it_removes() -> None:
    assert clear_label(1) == "✕  Clear 1 filter"
    assert clear_label(3) == "✕  Clear 3 filters"


@pytest.mark.asyncio
async def test_a_source_that_overrides_its_defaults_can_return_to_them(
    tmp_index_dir: Path,
) -> None:
    app = FNDApp(index_dir=tmp_index_dir)
    async with app.run_test(size=(110, 30)) as pilot:
        await pilot.pause()
        await _browser(app, pilot, FilterSpec(kinds=("md",)), inherited=_DEFAULTS)
        visible = app.screen.query_one("#clear_filters_bar", ClearFiltersBar).visible
        on_screen = _painted(app)

    assert visible, "a source that departed from its defaults offered no way back"
    assert RETURN_TO_DEFAULTS in on_screen, "the row never reached the screen"


@pytest.mark.asyncio
async def test_a_source_sitting_on_its_defaults_offers_no_row(tmp_index_dir: Path) -> None:
    """The control: nothing to return to means no row."""
    app = FNDApp(index_dir=tmp_index_dir)
    async with app.run_test(size=(110, 30)) as pilot:
        await pilot.pause()
        await _browser(app, pilot, _DEFAULTS[0], inherited=_DEFAULTS)
        visible = app.screen.query_one("#clear_filters_bar", ClearFiltersBar).visible

    assert not visible


@pytest.mark.asyncio
async def test_the_global_set_offers_no_row_at_all(tmp_index_dir: Path) -> None:
    """It inherits from nothing, so there is nothing to return to. Asking
    whether clearing would change something instead put the row on an
    untouched screen, permanently, reading "Clear"."""
    app = FNDApp(index_dir=tmp_index_dir)
    async with app.run_test(size=(110, 30)) as pilot:
        await pilot.pause()
        await _browser(app, pilot, _DEFAULTS[0])
        visible = app.screen.query_one("#clear_filters_bar", ClearFiltersBar).visible

    assert not visible


@pytest.mark.asyncio
async def test_up_from_the_top_row_reaches_it(tmp_index_dir: Path) -> None:
    """Arrow keys alone must find it, as they do in the sidebar."""
    app = FNDApp(index_dir=tmp_index_dir)
    async with app.run_test(size=(110, 30)) as pilot:
        await pilot.pause()
        await _browser(app, pilot, FilterSpec(kinds=("md",)), inherited=_DEFAULTS)
        tree = app.screen.query_one("#filter_tree", ToggleTree)
        tree.focus()
        tree.cursor_line = 0
        for _ in range(4):
            await pilot.pause()
        await pilot.press("up")
        for _ in range(4):
            await pilot.pause()
        focused = app.screen.focused

    assert isinstance(focused, ClearFiltersBar), focused


@pytest.mark.asyncio
async def test_enter_on_the_row_returns_and_hands_focus_back(tmp_index_dir: Path) -> None:
    app = FNDApp(index_dir=tmp_index_dir)
    async with app.run_test(size=(110, 30)) as pilot:
        await pilot.pause()
        screen = await _browser(app, pilot, FilterSpec(kinds=("md",)), inherited=_DEFAULTS)
        bar = app.screen.query_one("#clear_filters_bar", ClearFiltersBar)
        bar.focus()
        for _ in range(4):
            await pilot.pause()
        await pilot.press("enter")
        for _ in range(8):
            await pilot.pause()
        spec, focused, visible = screen._spec, app.screen.focused, bar.visible

    assert spec == _DEFAULTS[0], spec
    assert not visible, "the row stayed after there was nothing left to return from"
    assert isinstance(focused, ToggleTree), focused


def test_both_panes_answer_to_the_same_gesture() -> None:
    """Not a second letter for the same act. One pane cleared on `X` from
    anywhere and the other on `c`, and `c` sat beside `t` and `y`, one
    unconfirmed keystroke from wiping the set."""
    from textual.binding import Binding

    from fnd.tui.actions import load_keymap
    from fnd.tui.settings_screen import _CLEAR_FILTERS_KEY

    sidebar = load_keymap().for_action("clear_filters")
    bound = {
        b.key
        for b in FilterBrowserScreen.BINDINGS
        if isinstance(b, Binding) and b.action == "clear_all"
    }

    assert sidebar, "the sidebar's own clear action lost its key"
    assert _CLEAR_FILTERS_KEY == sidebar, (_CLEAR_FILTERS_KEY, sidebar)
    assert bound == {sidebar}, bound


def test_neither_pane_builds_the_label_itself() -> None:
    """One builder per act, not one string copied into two modules."""
    import inspect

    from fnd.tui import scope_panel, settings_screen

    assert "clear_label(" in inspect.getsource(scope_panel)
    assert "RETURN_TO_DEFAULTS" in inspect.getsource(settings_screen)
    for module in (scope_panel, settings_screen):
        assert "filter{" not in inspect.getsource(module), module.__name__


class TestTheKeyAndTheRowAgree:
    """A hidden row with a live key is a destructive gesture nobody can see.

    The global set inherits from nothing, so the row is correctly hidden, and
    a key left bound would empty the shipped never-index exclusion. The browser
    cannot put that tag back: a tag is only offered as a row while some file
    still carries it, so the only way back is the raw text editor.
    """

    @pytest.mark.asyncio
    async def test_the_global_set_cannot_be_emptied_by_the_key(self, tmp_index_dir: Path) -> None:
        app = FNDApp(index_dir=tmp_index_dir)
        async with app.run_test(size=(110, 30)) as pilot:
            await pilot.pause()
            screen = await _browser(app, pilot, _DEFAULTS[0])
            key = app._fnd_keymap.for_action("clear_filters") or "X"
            await pilot.press(key)
            for _ in range(8):
                await pilot.pause()
            spec = screen._spec

        assert spec.tag_excludes.get("frontmatter") == ("no_index",), (
            f"the never-index exclusion was dropped with no way back: {spec}"
        )

    @pytest.mark.asyncio
    async def test_the_key_is_hidden_wherever_the_row_is(self, tmp_index_dir: Path) -> None:
        """One predicate, so a footer cannot advertise what a screen refuses."""
        app = FNDApp(index_dir=tmp_index_dir)
        async with app.run_test(size=(110, 30)) as pilot:
            await pilot.pause()
            screen = await _browser(app, pilot, _DEFAULTS[0])
            global_row = app.screen.query_one("#clear_filters_bar", ClearFiltersBar).visible
            global_key = screen.check_action("clear_all", ())
            global_footer = _painted(app)

            screen = await _browser(app, pilot, FilterSpec(kinds=("md",)), inherited=_DEFAULTS)
            source_row = app.screen.query_one("#clear_filters_bar", ClearFiltersBar).visible
            source_key = screen.check_action("clear_all", ())

        assert not global_row
        assert not global_key, "the row was hidden and the key stayed live"
        assert "Return to defaults" not in global_footer, global_footer.splitlines()[-1]
        assert source_row
        assert source_key, "the row was offered and the key refused"

    @pytest.mark.asyncio
    async def test_a_source_can_still_return(self, tmp_index_dir: Path) -> None:
        """The control: disabling it where there is nothing to return to must
        not disable it where there is."""
        app = FNDApp(index_dir=tmp_index_dir)
        async with app.run_test(size=(110, 30)) as pilot:
            await pilot.pause()
            screen = await _browser(app, pilot, FilterSpec(kinds=("md",)), inherited=_DEFAULTS)
            key = app._fnd_keymap.for_action("clear_filters") or "X"
            await pilot.press(key)
            for _ in range(8):
                await pilot.pause()
            spec = screen._spec

        assert spec == _DEFAULTS[0], spec


class TestSavingNothingSaysNothingWasSaved:
    """`^s` on an untouched set toasted `Filters saved.` over a byte-identical
    config, while the exit guard called that same state clean and left without
    asking. Two answers to one question, and the save path reindexes, so the
    lie cost a rebuild as well.
    """

    @pytest.mark.asyncio
    async def test_an_untouched_set_is_not_written(self, tmp_index_dir: Path) -> None:
        saved: list[object] = []
        app = FNDApp(index_dir=tmp_index_dir)
        async with app.run_test(size=(110, 30)) as pilot:
            await pilot.pause()
            screen = FilterBrowserScreen(
                title="Index filters",
                spec=_DEFAULTS[0],
                gitignore=True,
                fndignore=True,
                sample_provider=lambda _spec: _SAMPLE,
                on_save=lambda *a: saved.append(a),
            )
            app.push_screen(screen)
            for _ in range(25):
                await pilot.pause()
            assert not screen._dirty(), "the premise: nothing has been touched"
            screen.action_save_close()
            for _ in range(8):
                await pilot.pause()

        assert not saved, "it wrote and reindexed a set nobody had changed"

    @pytest.mark.asyncio
    async def test_a_changed_set_still_is(self, tmp_index_dir: Path) -> None:
        """The control: the guard is on 'nothing changed', not on saving."""
        saved: list[object] = []
        app = FNDApp(index_dir=tmp_index_dir)
        async with app.run_test(size=(110, 30)) as pilot:
            await pilot.pause()
            screen = FilterBrowserScreen(
                title="Index filters",
                spec=_DEFAULTS[0],
                gitignore=True,
                fndignore=True,
                sample_provider=lambda _spec: _SAMPLE,
                on_save=lambda *a: saved.append(a),
            )
            app.push_screen(screen)
            for _ in range(25):
                await pilot.pause()
            screen._spec = FilterSpec(kinds=("md",))
            assert screen._dirty(), "the premise"
            screen.action_save_close()
            for _ in range(8):
                await pilot.pause()

        assert len(saved) == 1, saved

    @pytest.mark.asyncio
    async def test_the_guard_and_the_save_agree(self, tmp_index_dir: Path) -> None:
        """Both read one predicate, so neither can call a state clean while
        the other calls it worth writing."""
        app = FNDApp(index_dir=tmp_index_dir)
        async with app.run_test(size=(110, 30)) as pilot:
            await pilot.pause()
            screen = await _browser(app, pilot, _DEFAULTS[0])
            clean_guard = screen.unsaved_work()
            screen._spec = FilterSpec(kinds=("md",))
            dirty_guard = screen.unsaved_work()

        assert clean_guard is None
        assert dirty_guard is not None
