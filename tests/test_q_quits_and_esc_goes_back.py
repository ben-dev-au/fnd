"""`q` quits, `Esc` goes back, and neither loses unsaved work.

`q` meant "back" on six editing screens and "quit" everywhere else (one key,
two meanings, a screen apart) because nothing stopped a quit throwing an edit
away. Esc already did back on all eighteen, so `q` was duplicating it. With
the prompt in place, `q` can mean one thing.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from fnd.config import CollectionConfig, Config, SourceConfig
from fnd.tui import FNDApp
from fnd.tui.settings_screen import SourceFormScreen, UnsavedChangesScreen

_MODULE = Path(__file__).resolve().parent.parent / "fnd" / "tui" / "settings_screen.py"


def _app(tmp_path: Path) -> FNDApp:
    corpus = tmp_path / "notes"
    corpus.mkdir(exist_ok=True)
    return FNDApp(
        index_dir=tmp_path / "idx",
        config=Config(collections={"notes": CollectionConfig(sources=[SourceConfig(path=corpus)])}),
    )


#: The one screen where `q` must NOT quit: it exists to intercept the quit, so
#: letting the key through would answer its own question.
_QUIT_GATE = "UnsavedChangesScreen"


def test_no_settings_screen_binds_q_to_back() -> None:
    """Class-wide: Esc is back on all of them, so a second key for it is a
    second meaning for `q`. The quit gate is the exception, and the test below
    holds it to what it is for."""
    source = _MODULE.read_text(encoding="utf-8")
    offenders = [
        node.name
        for node in ast.parse(source).body
        if isinstance(node, ast.ClassDef)
        and node.name != _QUIT_GATE
        and re.search(r'Binding\(\s*"q"\s*,\s*"back"', ast.get_source_segment(source, node) or "")
    ]
    assert not offenders, f"screens where q means back, not quit: {offenders}"


def test_the_quit_gate_is_the_only_exception_and_still_swallows_q() -> None:
    """The carve-out is one screen and it is load-bearing: `q` raised the
    question, and reaching the app's quit through the gate answered it."""
    from textual.binding import Binding

    from fnd.tui.settings_screen import UnsavedChangesScreen

    bound = {
        b.action for b in UnsavedChangesScreen.BINDINGS if isinstance(b, Binding) and b.key == "q"
    }
    assert bound == {"back"}, bound


@pytest.mark.asyncio
async def test_q_on_a_clean_form_quits(tmp_path: Path) -> None:
    app = _app(tmp_path)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app.push_screen(SourceFormScreen(collection_name="notes", source_index=0))
        for _ in range(20):
            await pilot.pause()
        await pilot.press("q")
        await pilot.pause()
        exiting = app._exit

    assert exiting, "q on a screen with nothing to lose must quit"


@pytest.mark.asyncio
async def test_q_on_a_dirty_form_asks_first(tmp_path: Path) -> None:
    app = _app(tmp_path)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app.push_screen(SourceFormScreen(collection_name="notes", source_index=0))
        for _ in range(20):
            await pilot.pause()
        form = app.screen
        assert isinstance(form, SourceFormScreen)
        form._fields["excludes_custom"] = "build/**"
        await pilot.press("q")
        await pilot.pause()
        asked = isinstance(app.screen, UnsavedChangesScreen)
        exiting = app._exit

    assert asked, "q must not throw away an edit"
    assert not exiting, "and must not have quit yet"


@pytest.mark.asyncio
async def test_esc_still_goes_back_not_out(tmp_path: Path) -> None:
    """The control: the two keys must not collapse into each other."""
    app = _app(tmp_path)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app.push_screen(SourceFormScreen(collection_name="notes", source_index=0))
        for _ in range(20):
            await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        left_form = not isinstance(app.screen, SourceFormScreen)
        exiting = app._exit

    assert left_form, "Esc leaves the screen"
    assert not exiting, "Esc does not quit the app"
