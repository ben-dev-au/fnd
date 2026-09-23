"""Leaving a form with an invalid field offered "Save changes" as the default.

Choosing it ran the save, which refused, popped nothing and repainted nothing
(the error line was already on screen from the last attempt), so the prompt came
straight back. Enter on the default looped; the only exit was Discard.
"""

from __future__ import annotations

import ast
import textwrap
from pathlib import Path
from typing import Any

import pytest
from textual.widgets import OptionList

from fnd.config import CollectionConfig, SourceConfig, load, write_collection
from fnd.tui import FNDApp
from fnd.tui.settings_screen import (
    AddCollectionWizard,
    SourceFormScreen,
    UnsavedChangesScreen,
)


@pytest.fixture
def app_with_a_source(
    tmp_path: Path, tmp_index_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> FNDApp:
    cfg_path = tmp_path / "config.toml"
    root = tmp_path / "vault"
    root.mkdir()
    monkeypatch.setattr("fnd.config.default_config_path", lambda: cfg_path)
    write_collection(
        config_path=cfg_path,
        name="probe",
        collection=CollectionConfig(sources=[SourceConfig(path=root)]),
    )
    return FNDApp(index_dir=tmp_index_dir, config=load(cfg_path))


def _option_ids(screen: UnsavedChangesScreen) -> list[str | None]:
    return [o.id for o in screen.query_one("#confirm_list", OptionList)._options]


async def _form_left_with(app: FNDApp, pilot: Any, path: str) -> UnsavedChangesScreen:
    app.push_screen(SourceFormScreen(collection_name="probe", source_index=0))
    for _ in range(30):
        await pilot.pause()
    form = app.screen
    assert isinstance(form, SourceFormScreen)
    form._fields["path"] = path
    await pilot.press("escape")
    for _ in range(10):
        await pilot.pause()
    prompt = app.screen
    assert isinstance(prompt, UnsavedChangesScreen)
    return prompt


@pytest.mark.asyncio
async def test_a_save_that_cannot_work_is_not_offered(app_with_a_source: FNDApp) -> None:
    app = app_with_a_source
    async with app.run_test(size=(110, 40)) as pilot:
        await pilot.pause()
        prompt = await _form_left_with(app, pilot, "/nope/does/not/exist")
        ids = _option_ids(prompt)
        painted = "\n".join(
            "".join(s.text for s in strip) for strip in app.screen._compositor.render_strips()
        )

    assert "save" not in ids, ids
    assert "Cannot save yet" in painted, painted
    assert "/nope/does/not/exist" in painted, "it must name the field it is about"


@pytest.mark.asyncio
async def test_the_cursor_lands_on_the_way_back(app_with_a_source: FNDApp) -> None:
    """With Save gone the first row is Discard, and Enter is one keypress."""
    app = app_with_a_source
    async with app.run_test(size=(110, 40)) as pilot:
        await pilot.pause()
        prompt = await _form_left_with(app, pilot, "/nope/does/not/exist")
        options = prompt.query_one("#confirm_list", OptionList)
        highlighted = options.highlighted
        ids = _option_ids(prompt)

        await pilot.press("enter")
        for _ in range(10):
            await pilot.pause()
        landed = app.screen

    assert highlighted is not None
    assert ids[highlighted] == "stay"
    assert isinstance(landed, SourceFormScreen), "Enter must return to the form, not discard it"


@pytest.mark.asyncio
async def test_a_form_that_can_save_still_offers_it(
    app_with_a_source: FNDApp, tmp_path: Path
) -> None:
    """The control: nothing is withheld from a form that would save."""
    app = app_with_a_source
    async with app.run_test(size=(110, 40)) as pilot:
        await pilot.pause()
        prompt = await _form_left_with(app, pilot, str(tmp_path))
        ids = _option_ids(prompt)
        highlighted = prompt.query_one("#confirm_list", OptionList).highlighted

    assert ids[0] == "save", "the save must still be OFFERED, which is the point"
    # The landing is off Save: this prompt is reached by a key the footer
    # offers as a way out, so Enter must not write.
    assert ids[highlighted or 0] == "stay", ids


@pytest.mark.asyncio
async def test_the_wizard_answers_the_same_way(app_with_a_source: FNDApp) -> None:
    app = app_with_a_source
    async with app.run_test(size=(110, 40)) as pilot:
        await pilot.pause()
        app.push_screen(AddCollectionWizard())
        for _ in range(30):
            await pilot.pause()
        wizard = app.screen
        assert isinstance(wizard, AddCollectionWizard)
        wizard._fields["name"] = "probe"  # already taken
        assert "already exists" in wizard.save_blocked()

        wizard._fields["name"] = ""
        assert wizard.save_blocked() == "Name is required."


def test_the_save_and_the_prompt_read_one_answer() -> None:
    """They disagreed once already: the prompt offered what the save refused."""
    module = Path("fnd/tui/settings_screen.py").read_text(encoding="utf-8")
    tree = ast.parse(module)
    checked = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or node.name not in (
            "SourceFormScreen",
            "AddCollectionWizard",
        ):
            continue
        save = next(
            f for f in node.body if isinstance(f, ast.FunctionDef) and f.name == "action_save_close"
        )
        body = ast.get_source_segment(module, save) or ""
        assert "self.save_blocked()" in body, f"{node.name} validates on its own"
        checked += 1
    assert checked == 2


@pytest.mark.asyncio
async def test_a_buried_form_says_why_it_cannot_be_saved(
    tmp_path: Path, tmp_index_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A form under another editor cannot be saved from the prompt. That was
    already true; the prompt just never said so, and defaulted to Discard."""
    from fnd.tui.settings_screen import unsaved_on_stack

    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        textwrap.dedent(f"""
            [[collections.probe.sources]]
            path = "{tmp_path.as_posix()}"
        """),
        encoding="utf-8",
    )
    monkeypatch.setattr("fnd.config.default_config_path", lambda: cfg_path)
    app = FNDApp(index_dir=tmp_index_dir, config=load(cfg_path))
    async with app.run_test(size=(110, 40)) as pilot:
        await pilot.pause()
        app.push_screen(SourceFormScreen(collection_name="probe", source_index=0))
        for _ in range(30):
            await pilot.pause()
        form = app.screen
        assert isinstance(form, SourceFormScreen)
        form._fields["excludes_custom"] = "build/**"
        app.push_screen(AddCollectionWizard())
        for _ in range(20):
            await pilot.pause()
        pending = unsaved_on_stack(app.screen_stack)

    assert pending is not None
    what, save, blocked = pending
    assert what == "this source"
    assert save is None
    assert blocked, "no save offered and no reason given"
