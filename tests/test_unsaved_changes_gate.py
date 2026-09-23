"""Leaving a screen with unsaved work asks before it throws the work away.

Surveyed across all eighteen settings screens: seven lost unsaved work
silently on Esc, one said "discarded" after it was gone, and none prompted.
Esc was the one gesture consistent on all eighteen, and on seven of them it
destroyed work with no signal, so knowing which key saves was a precondition
for not losing data.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Callable
from pathlib import Path

import pytest
from textual.app import ComposeResult
from textual.screen import Screen
from textual.widgets import Static

from fnd.config import CollectionConfig, Config, SourceConfig
from fnd.tui import FNDApp
from fnd.tui.settings_screen import SourceFormScreen, UnsavedChangesScreen

_MODULE = Path(__file__).resolve().parent.parent / "fnd" / "tui" / "settings_screen.py"


def _app(tmp_path: Path) -> FNDApp:
    corpus = tmp_path / "notes"
    corpus.mkdir(exist_ok=True)
    config = Config(collections={"notes": CollectionConfig(sources=[SourceConfig(path=corpus)])})
    return FNDApp(index_dir=tmp_path / "idx", config=config)


@pytest.mark.asyncio
async def test_a_dirty_form_asks_instead_of_discarding(tmp_path: Path) -> None:
    app = _app(tmp_path)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app.push_screen(SourceFormScreen(collection_name="notes", source_index=0))
        for _ in range(20):
            await pilot.pause()
        form = app.screen
        assert isinstance(form, SourceFormScreen)
        form._fields["excludes_custom"] = "build/**"
        await pilot.press("escape")
        await pilot.pause()
        asked = isinstance(app.screen, UnsavedChangesScreen)

    assert asked, "Esc on a changed form must not leave silently"


@pytest.mark.asyncio
async def test_an_untouched_form_leaves_without_asking(tmp_path: Path) -> None:
    """The control: nothing to lose, nothing to ask about."""
    app = _app(tmp_path)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app.push_screen(SourceFormScreen(collection_name="notes", source_index=0))
        for _ in range(20):
            await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        left = not isinstance(app.screen, (SourceFormScreen, UnsavedChangesScreen))

    assert left, "an unchanged form must not stop the user"


@pytest.mark.asyncio
async def test_keep_editing_returns_to_the_form(tmp_path: Path) -> None:
    app = _app(tmp_path)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app.push_screen(SourceFormScreen(collection_name="notes", source_index=0))
        for _ in range(20):
            await pilot.pause()
        form = app.screen
        assert isinstance(form, SourceFormScreen)
        form._fields["excludes_custom"] = "build/**"
        await pilot.press("escape")
        await pilot.pause()
        await pilot.press("escape")  # Esc on the prompt = keep editing
        await pilot.pause()
        back_on_form = app.screen is form
        kept = form._fields["excludes_custom"]

    assert back_on_form, "Esc on the prompt must return to the form"
    assert kept == "build/**", "and must not have discarded the edit"


@pytest.mark.asyncio
async def test_discard_leaves_and_drops_the_edit(tmp_path: Path) -> None:
    app = _app(tmp_path)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app.push_screen(SourceFormScreen(collection_name="notes", source_index=0))
        for _ in range(20):
            await pilot.pause()
        form = app.screen
        assert isinstance(form, SourceFormScreen)
        form._fields["excludes_custom"] = "build/**"
        await pilot.press("escape")
        await pilot.pause()
        # Reach Discard from wherever the prompt lands, and say so: it lands on
        # the row that changes nothing, so a relative `down` from an assumed
        # landing can silently miss Discard.
        from textual.widgets import OptionList

        prompt = app.screen
        assert isinstance(prompt, UnsavedChangesScreen)
        options = prompt.query_one("#confirm_list", OptionList)
        options.highlighted = next(i for i, o in enumerate(options._options) if o.id == "discard")
        await pilot.press("enter")
        for _ in range(4):
            await pilot.pause()
        gone = not isinstance(app.screen, (SourceFormScreen, UnsavedChangesScreen))

    assert gone, "Discard must leave the form"


def test_no_editing_screen_leaves_unsaved_work_silently() -> None:
    """Class-wide: a screen that computes dirtiness must route Esc through the
    prompt, so the next editing screen cannot opt out by forgetting."""
    source = _MODULE.read_text(encoding="utf-8")
    offenders: list[str] = []
    for node in ast.parse(source).body:
        if not isinstance(node, ast.ClassDef):
            continue
        back = next(
            (
                ast.get_source_segment(source, f) or ""
                for f in node.body
                if isinstance(f, ast.FunctionDef) and f.name == "action_back"
            ),
            "",
        )
        computes_dirty = bool(re.search(r"_dirty\(\)|_snapshot != |_opened_with", back))
        if computes_dirty and "_leave_or_confirm" not in back:
            offenders.append(node.name)
    assert not offenders, f"screens that decide about unsaved work without asking: {offenders}"


def test_every_screen_can_actually_be_left() -> None:
    """A screen whose Esc neither pops nor asks is a trap with no way out.

    A method inserted into the middle of `action_back` can strand its exit as
    dead code, so the filter browser cannot be left once its search box is
    empty. An end-to-end test catches one instance; this catches the shape.
    """
    source = _MODULE.read_text(encoding="utf-8")
    trapped: list[str] = []
    for node in ast.parse(source).body:
        if not isinstance(node, ast.ClassDef):
            continue
        back = next(
            (f for f in node.body if isinstance(f, ast.FunctionDef) and f.name == "action_back"),
            None,
        )
        if back is None:
            continue
        body = ast.get_source_segment(source, back) or ""
        if "pop_screen" not in body and "_leave_or_confirm" not in body:
            trapped.append(node.name)
    assert not trapped, f"screens with no way out of action_back: {trapped}"


def test_no_method_hides_code_after_its_return() -> None:
    """The mechanism of that bug, class-wide: an edit that lands inside
    another method leaves its tail unreachable and silent."""
    source = _MODULE.read_text(encoding="utf-8")
    dead: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.FunctionDef):
            continue
        for index, stmt in enumerate(node.body[:-1]):
            if isinstance(stmt, ast.Return) and index < len(node.body) - 1:
                dead.append(f"{node.name}:{stmt.lineno}")
    assert not dead, f"unreachable code after a return: {dead}"


class TestTheGateCoversTheWholeStack:
    """The gate asks every screen in the stack, and `q` cannot answer it.

    The filter browser is only ever pushed on top of the source form, so a gate
    asking only the top screen lets a dirty form under a clean browser quit
    with no prompt. And `q` reaching the app's quit THROUGH the gate would
    answer its question by pressing the same key again.
    """

    @pytest.mark.asyncio
    async def test_a_dirty_screen_under_a_clean_one_still_stops_the_quit(
        self, tmp_index_dir: Path
    ) -> None:
        from fnd.filters import FilterSpec
        from fnd.tui.settings_screen import FilterBrowserScreen, UnsavedChangesScreen

        app = FNDApp(index_dir=tmp_index_dir)
        async with app.run_test(size=(110, 30)) as pilot:
            await pilot.pause()
            app.push_screen(_DirtyScreen())
            for _ in range(6):
                await pilot.pause()
            app.push_screen(
                FilterBrowserScreen(
                    title="Index filters",
                    spec=FilterSpec(),
                    gitignore=True,
                    fndignore=True,
                    on_save=lambda *_a: None,
                )
            )
            for _ in range(10):
                await pilot.pause()
            app.action_quit()
            for _ in range(8):
                await pilot.pause()
            asked = isinstance(app.screen, UnsavedChangesScreen)
            running = app.is_running

        assert asked, "it quit with a dirty screen buried in the stack"
        assert running

    @pytest.mark.asyncio
    async def test_the_gate_does_not_offer_to_save_what_it_cannot_reach(
        self, tmp_index_dir: Path
    ) -> None:
        """A form buried under another editor cannot be saved from a modal:
        its own save pops whatever is on top, which is not it."""
        from textual.widgets import OptionList

        from fnd.filters import FilterSpec
        from fnd.tui.settings_screen import FilterBrowserScreen

        app = FNDApp(index_dir=tmp_index_dir)
        async with app.run_test(size=(110, 30)) as pilot:
            await pilot.pause()
            app.push_screen(_DirtyScreen())
            for _ in range(6):
                await pilot.pause()
            app.push_screen(
                FilterBrowserScreen(
                    title="Index filters",
                    spec=FilterSpec(),
                    gitignore=True,
                    fndignore=True,
                    on_save=lambda *_a: None,
                )
            )
            for _ in range(10):
                await pilot.pause()
            app.action_quit()
            for _ in range(8):
                await pilot.pause()
            ids = [o.id for o in app.screen.query_one("#confirm_list", OptionList)._options]

        assert "save" not in ids, ids
        assert "discard" in ids, ids
        assert "stay" in ids, ids

    @pytest.mark.asyncio
    async def test_q_on_the_gate_keeps_editing_rather_than_quitting(
        self, tmp_index_dir: Path
    ) -> None:
        from fnd.tui.settings_screen import UnsavedChangesScreen

        app = FNDApp(index_dir=tmp_index_dir)
        async with app.run_test(size=(110, 30)) as pilot:
            await pilot.pause()
            app.push_screen(_DirtyScreen())
            for _ in range(6):
                await pilot.pause()
            app.action_quit()
            for _ in range(8):
                await pilot.pause()
            assert isinstance(app.screen, UnsavedChangesScreen), "the premise"
            await pilot.press("q")
            for _ in range(8):
                await pilot.pause()
            running = app.is_running
            gone = app.screen.__class__ is not UnsavedChangesScreen

        assert running, "the key that raised the question answered it"
        assert gone, "and it should still dismiss the gate"


class _DirtyScreen(Screen[None]):
    """A screen that always has something to lose."""

    def compose(self) -> ComposeResult:
        yield Static("dirty")

    def unsaved_work(self) -> tuple[str, Callable[[], None]] | None:
        return "This form", lambda: None


class TestEveryAdvertisedExitAsks:
    """Every exit the footer advertises asks first, `:` included.

    Esc, ←, `q` and the menu are the obvious exits; `:` is the key the screen's
    own footer names, and ungated it closes the whole settings stack in silence.
    """

    @pytest.mark.asyncio
    async def test_the_colon_key_asks_before_discarding(self, tmp_index_dir: Path) -> None:
        app = FNDApp(index_dir=tmp_index_dir)
        async with app.run_test(size=(110, 30)) as pilot:
            await pilot.pause()
            app.push_screen(_DirtyScreen())
            for _ in range(6):
                await pilot.pause()
            app._close_settings_stack()
            for _ in range(8):
                await pilot.pause()
            asked = app.screen.__class__ is UnsavedChangesScreen

        assert asked, "`:` closed the stack without asking"

    @pytest.mark.asyncio
    async def test_a_clean_stack_still_closes_at_once(self, tmp_index_dir: Path) -> None:
        """The control: the gate must not put a prompt in front of a user who
        has nothing to lose."""
        from fnd.tui.settings_screen import SettingsScreen, open_settings

        app = FNDApp(index_dir=tmp_index_dir)
        async with app.run_test(size=(110, 30)) as pilot:
            await pilot.pause()
            open_settings(app)
            for _ in range(15):
                await pilot.pause()
            assert isinstance(app.screen, SettingsScreen), "the premise"
            app._close_settings_stack()
            for _ in range(8):
                await pilot.pause()
            # By name: pyright narrows `app.screen` from the assert above, so
            # an isinstance here reads as always-true and never runs.
            names = [t.__name__ for t in type(app.screen).__mro__]
            closed = SettingsScreen.__name__ not in names
            prompted = UnsavedChangesScreen.__name__ in names

        assert closed, "a clean settings stack did not close"
        assert not prompted

    @pytest.mark.asyncio
    async def test_discarding_from_the_gate_does_close_it(self, tmp_index_dir: Path) -> None:
        """And the prompt must not become a second thing to escape from,
        driven through the option list, which is what the user presses."""
        from textual.widgets import OptionList

        app = FNDApp(index_dir=tmp_index_dir)
        async with app.run_test(size=(110, 30)) as pilot:
            await pilot.pause()
            app.push_screen(_DirtyScreen())
            for _ in range(6):
                await pilot.pause()
            app._close_settings_stack()
            for _ in range(8):
                await pilot.pause()
            assert app.screen.__class__ is UnsavedChangesScreen, "the premise"
            options = app.screen.query_one("#confirm_list", OptionList)
            options.highlighted = next(
                i for i, o in enumerate(options._options) if o.id == "discard"
            )
            await pilot.pause()
            options.action_select()
            for _ in range(10):
                await pilot.pause()
            gone = app.screen.__class__ is not UnsavedChangesScreen
            running = app.is_running

        assert gone, "the gate stayed up after discard"
        assert running, "discard should close the stack, not the app"


class TestOpeningAStackIsAlsoAnExit:
    """Opening a settings stack over a dirty editor loses the edit as surely
    as closing one does.

    The filter browser is not a `SettingsScreen`, so `:` fell through to the
    open branch and pushed a SECOND stack over it, from which a second Index
    filters could be opened and saved, leaving the first holding stale values
    that its own `^s` then wrote back over the newer save.
    """

    @pytest.mark.asyncio
    async def test_the_menu_key_asks_before_opening_over_a_dirty_screen(
        self, tmp_index_dir: Path
    ) -> None:
        app = FNDApp(index_dir=tmp_index_dir)
        async with app.run_test(size=(110, 30)) as pilot:
            await pilot.pause()
            app.push_screen(_DirtyScreen())
            for _ in range(6):
                await pilot.pause()
            app.action_open_command_palette()
            for _ in range(8):
                await pilot.pause()
            asked = app.screen.__class__ is UnsavedChangesScreen

        assert asked, "`:` opened a second stack over an unsaved edit"

    @pytest.mark.asyncio
    async def test_a_clean_screen_still_opens_at_once(self, tmp_index_dir: Path) -> None:
        """The control: nothing to lose means no prompt in the way."""
        from fnd.tui.settings_screen import SettingsScreen

        app = FNDApp(index_dir=tmp_index_dir)
        async with app.run_test(size=(110, 30)) as pilot:
            await pilot.pause()
            app.action_open_command_palette()
            for _ in range(12):
                await pilot.pause()
            names = [t.__name__ for t in type(app.screen).__mro__]

        assert SettingsScreen.__name__ in names, app.screen
        assert UnsavedChangesScreen.__name__ not in names
