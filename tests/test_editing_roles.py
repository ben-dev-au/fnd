"""The three roles behave as the spec says, on toy screens."""

from __future__ import annotations

import pytest
from textual.app import App, ComposeResult
from textual.screen import Screen
from textual.widgets import Static

from fnd.tui.editing import (
    DocumentScreen,
    Leave,
    PartScreen,
    ask_before_leaving,
    pending_document,
)


class _Doc(DocumentScreen):
    SUBJECT = "this toy"

    def __init__(self) -> None:
        super().__init__()
        self.value = 0
        self.saved: list[int] = []
        self.opened = 0
        self.blocked = ""

    def compose(self) -> ComposeResult:
        yield Static("doc")

    def is_dirty(self) -> bool:
        return self.value != self.opened

    def blocked_reason(self) -> str:
        return self.blocked

    def write(self) -> str:
        self.saved.append(self.value)
        self.opened = self.value
        return ""


class _Part(PartScreen):
    def __init__(self, doc: _Doc, typed: int | None) -> None:
        super().__init__()
        self.doc, self.typed = doc, typed

    def compose(self) -> ComposeResult:
        yield Static("part")

    def hand_back(self) -> str:
        if self.typed is None:
            return "the typed value has an error"
        self.doc.value = self.typed
        return ""


class _Host(App[None]):
    def compose(self) -> ComposeResult:
        yield Static("base")


async def _settle(pilot: object) -> None:
    for _ in range(3):
        await pilot.pause()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_esc_on_a_clean_document_goes_back() -> None:
    """Nothing to lose, nothing to ask."""
    app = _Host()
    async with app.run_test() as pilot:
        app.push_screen(_Doc())
        await _settle(pilot)
        await pilot.press("escape")
        await _settle(pilot)
        assert not isinstance(app.screen, _Doc)


@pytest.mark.asyncio
async def test_esc_on_a_dirty_document_asks_once_and_names_the_way_out() -> None:
    """The prompt reads Save and go back, Discard and go back, Keep editing."""
    from fnd.tui.settings_screen import UnsavedChangesScreen

    app = _Host()
    async with app.run_test() as pilot:
        doc = _Doc()
        app.push_screen(doc)
        await _settle(pilot)
        doc.value = 1
        await pilot.press("escape")
        await _settle(pilot)
        screen = app.screen
        assert isinstance(screen, UnsavedChangesScreen)
        assert screen.option_labels() == [
            "Save and go back",
            "Discard and go back",
            "Keep editing",
        ]


@pytest.mark.asyncio
async def test_a_blocked_document_offers_no_save_and_says_why() -> None:
    """A save certain to fail is never offered."""
    from fnd.tui.settings_screen import UnsavedChangesScreen

    app = _Host()
    async with app.run_test() as pilot:
        doc = _Doc()
        app.push_screen(doc)
        await _settle(pilot)
        doc.value, doc.blocked = 1, "Path is required."
        await pilot.press("escape")
        await _settle(pilot)
        screen = app.screen
        assert isinstance(screen, UnsavedChangesScreen)
        assert screen.option_labels() == ["Discard and go back", "Keep editing"]
        assert "Path is required." in screen.body_text()


@pytest.mark.asyncio
async def test_ctrl_s_writes_and_goes_back() -> None:
    """A document's ^s is the only save, and it leaves."""
    app = _Host()
    async with app.run_test() as pilot:
        doc = _Doc()
        app.push_screen(doc)
        await _settle(pilot)
        doc.value = 2
        await pilot.press("ctrl+s")
        await _settle(pilot)
        assert doc.saved == [2]
        assert not isinstance(app.screen, _Doc)


@pytest.mark.asyncio
async def test_discard_leaves_without_writing() -> None:
    """Discard and go back drops the working copy."""
    app = _Host()
    async with app.run_test() as pilot:
        doc = _Doc()
        app.push_screen(doc)
        await _settle(pilot)
        doc.value = 4
        await pilot.press("escape")
        await _settle(pilot)
        await pilot.press("up", "enter")
        await _settle(pilot)
        assert doc.saved == []
        assert not isinstance(app.screen, _Doc)


@pytest.mark.asyncio
async def test_a_part_carries_its_edit_back_on_esc_without_asking() -> None:
    """Esc on a part never discards and never prompts."""
    app = _Host()
    async with app.run_test() as pilot:
        doc = _Doc()
        app.push_screen(doc)
        app.push_screen(_Part(doc, typed=5))
        await _settle(pilot)
        await pilot.press("escape")
        await _settle(pilot)
        assert app.screen is doc
        assert doc.value == 5


@pytest.mark.asyncio
async def test_an_invalid_part_holds_then_a_second_esc_cancels_the_typing() -> None:
    """Never passes on something broken, and never traps the user."""
    app = _Host()
    async with app.run_test() as pilot:
        doc = _Doc()
        app.push_screen(doc)
        part = _Part(doc, typed=None)
        app.push_screen(part)
        await _settle(pilot)
        await pilot.press("escape")
        await _settle(pilot)
        assert app.screen is part
        await pilot.press("escape")
        await _settle(pilot)
        assert app.screen is doc
        assert doc.value == 0


def test_pending_document_hands_parts_back_first() -> None:
    """A dirty edit held in a part above the document still counts."""
    doc = _Doc()
    part = _Part(doc, typed=3)
    assert pending_document([Screen(), doc, part]) == (doc, "")
    assert doc.value == 3


def test_pending_document_reports_a_part_that_cannot_hand_back() -> None:
    """Quitting over invalid typing names the error instead of offering Save."""
    doc = _Doc()
    pending = pending_document([Screen(), doc, _Part(doc, typed=None)])
    assert pending == (doc, "the typed value has an error")


def test_no_document_means_nothing_pending() -> None:
    """Settings apply at once, so a stack with no document has nothing to lose."""
    assert pending_document([Screen(), Screen()]) is None


@pytest.mark.asyncio
async def test_save_from_the_prompt_runs_the_leave_even_when_buried() -> None:
    """Save no longer depends on which screen is on top."""
    app = _Host()
    left: list[str] = []
    async with app.run_test() as pilot:
        doc = _Doc()
        app.push_screen(doc)
        app.push_screen(_Part(doc, typed=7))
        await _settle(pilot)
        found = pending_document(app.screen_stack)
        assert found is not None
        ask_before_leaving(found[0], Leave("quit", lambda: left.append("quit")), blocked=found[1])
        await _settle(pilot)
        await pilot.press("up", "up", "enter")
        await _settle(pilot)
        assert doc.saved == [7]
        assert left == ["quit"]


def test_a_dirty_document_under_a_clean_one_is_still_found() -> None:
    """Saving no longer needs the document on top, so a buried one is asked about."""
    buried, top = _Doc(), _Doc()
    buried.value = 9
    assert pending_document([Screen(), buried, top]) == (buried, "")


async def _open_picker(app: object, pilot: object, item_id: str) -> object:
    from fnd.tui.menu import walk_all_sections
    from fnd.tui.settings_screen import PickerScreen

    item = next(i for _crumb, i in walk_all_sections(app) if i.id == item_id)  # type: ignore[arg-type]
    app.push_screen(PickerScreen(item))  # type: ignore[attr-defined]
    await _settle(pilot)
    return app.screen  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_a_multi_picker_applies_each_toggle(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tag sources is a setting: unticking applies at once, and Esc keeps it."""
    from pathlib import Path

    from fnd.config import Config, load
    from fnd.tui import FNDApp

    cfg_path = Path(str(tmp_path)) / "config.toml"
    cfg_path.write_text("", encoding="utf-8")
    monkeypatch.setattr("fnd.config.default_config_path", lambda: cfg_path)
    app = FNDApp(index_dir=Path(str(tmp_path)) / "idx", config=Config())
    async with app.run_test(size=(110, 30)) as pilot:
        await _settle(pilot)
        picker = await _open_picker(app, pilot, "filters.tag_sources")
        await pilot.press("enter")  # the cursor starts on a ticked source
        await _settle(pilot)
        await pilot.press("escape")
        await _settle(pilot)
        gone = app.screen is not picker
    assert gone
    assert len(load(cfg_path).defaults.tag_sources) == 1


def test_no_picker_binds_a_save() -> None:
    """A ^s on a picker would say Esc does not keep the choice."""
    from fnd.tui.settings_screen import PickerScreen

    for cls in (PickerScreen,):
        keys = {k for b in cls.BINDINGS for k in str(getattr(b, "key", "")).split(",")}
        assert "ctrl+s" not in keys, cls.__name__
