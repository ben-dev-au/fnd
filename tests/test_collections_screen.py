"""Phase 5.5e-3: TUI Collections form — F3 / :collections."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from textual.widgets import Static

from acorn.config import Config, load
from acorn.tui import AcornApp


@pytest.fixture
def cfg_with_one_collection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        textwrap.dedent("""
            [[collections.notes.sources]]
            path = "/tmp/notes"
            includes = ["**/*.md"]
        """),
        encoding="utf-8",
    )
    monkeypatch.setattr("acorn.config.default_config_path", lambda: cfg_path)
    monkeypatch.setattr("acorn.cli.default_config_path", lambda: cfg_path)
    return load(cfg_path)


@pytest.mark.asyncio
async def test_f3_opens_collections_screen(
    cfg_with_one_collection: Config, tmp_index_dir: Path
) -> None:
    app = AcornApp(index_dir=tmp_index_dir, config=cfg_with_one_collection)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("f3")
        await pilot.pause()
        # The screen mounts a Static with the title "Collections".
        # After push_screen, app.screen is the CollectionsScreen — query there.
        title = app.screen.query_one("#collections_title", Static)
        assert "collections" in str(title.content).lower()


@pytest.mark.asyncio
async def test_escape_closes_collections_screen(
    cfg_with_one_collection: Config, tmp_index_dir: Path
) -> None:
    app = AcornApp(index_dir=tmp_index_dir, config=cfg_with_one_collection)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("f3")
        await pilot.pause()
        assert app.screen.query("#collections_title")
        await pilot.press("escape")
        await pilot.pause()
        # After dismiss, app.screen is back to the main screen (no #collections_title).
        assert not app.screen.query("#collections_title")


@pytest.fixture
def cfg_three_collections(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        textwrap.dedent("""
            [[collections.papers.sources]]
            path = "/tmp/papers"

            [[collections.coursework.sources]]
            path = "/tmp/notes"
            includes = ["**/*.md"]

            [[collections.coursework.sources]]
            path = "/tmp/decks"
            includes = ["**/*.pdf"]

            [[collections.notes.sources]]
            path = "/tmp/zk"
        """),
        encoding="utf-8",
    )
    monkeypatch.setattr("acorn.config.default_config_path", lambda: cfg_path)
    return load(cfg_path)


@pytest.mark.asyncio
async def test_collections_list_shows_each_with_source_count(
    cfg_three_collections: Config, tmp_index_dir: Path
) -> None:
    app = AcornApp(index_dir=tmp_index_dir, config=cfg_three_collections)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("f3")
        await pilot.pause()
        # The list pane should have three rows (one per collection) showing
        # the name and source count.
        list_pane = app.screen.query_one("#collections_list_pane")
        statics = list_pane.query(Static)
        text = "\n".join(str(s.content) for s in statics)
        assert "papers" in text
        assert "coursework" in text
        assert "notes" in text
        assert "1 source" in text or "1 sources" in text
        assert "2 sources" in text


@pytest.mark.asyncio
async def test_clicking_collection_shows_its_sources(
    cfg_three_collections: Config, tmp_index_dir: Path
) -> None:
    app = AcornApp(index_dir=tmp_index_dir, config=cfg_three_collections)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("f3")
        await pilot.pause()
        # Default selection: first alphabetically (coursework). Editor pane
        # should already show its two sources without any extra interaction.
        editor = app.screen.query_one("#collections_editor_pane")
        text = "\n".join(str(s.content) for s in editor.query(Static))
        assert "/tmp/notes" in text
        assert "/tmp/decks" in text
        assert "**/*.md" in text
        assert "**/*.pdf" in text


@pytest.mark.asyncio
async def test_pressing_e_opens_source_edit_modal(
    cfg_three_collections: Config, tmp_index_dir: Path
) -> None:
    app = AcornApp(index_dir=tmp_index_dir, config=cfg_three_collections)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("f3")
        await pilot.pause()
        # Default selection: coursework (alphabetical first). Press 'e'
        # to edit the first source.
        await pilot.press("e")
        await pilot.pause()
        # Modal mounts an input with id source_path_input.
        assert app.screen.query("#source_path_input")


@pytest.mark.asyncio
async def test_invalid_filter_shows_parse_error(
    cfg_three_collections: Config, tmp_index_dir: Path
) -> None:
    app = AcornApp(index_dir=tmp_index_dir, config=cfg_three_collections)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("f3")
        await pilot.pause()
        await pilot.press("e")
        await pilot.pause()
        from textual.widgets import Input

        filter_input = app.screen.query_one("#source_filter_input", Input)
        filter_input.value = "Course =="  # invalid DSL
        # Filter parse-status should pick up the change after the input
        # event fires.
        await pilot.pause()
        status = app.screen.query_one("#filter_parse_status", Static)
        assert "col" in str(status.content).lower() or "error" in str(status.content).lower()
