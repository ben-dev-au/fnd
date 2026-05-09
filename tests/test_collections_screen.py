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
        # The tree's root has one child per collection; each label includes
        # the name + source count.
        from textual.widgets import Tree

        tree = app.screen.query_one("#collections_tree", Tree)
        labels = [str(c.label) for c in tree.root.children]
        text = "\n".join(labels)
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
        # Default cursor: first collection alphabetically (coursework).
        # Its two sources are children of that node and show automatically
        # because the form expands all collections by default.
        from textual.widgets import Tree

        tree = app.screen.query_one("#collections_tree", Tree)
        coursework = next(c for c in tree.root.children if "coursework" in str(c.label))
        source_labels = [str(s.label) for s in coursework.children]
        text = "\n".join(source_labels)
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
        # Default cursor lands on coursework (a collection node). Press
        # `j` once to descend to its first source, then `e` to edit.
        await pilot.press("j")
        await pilot.pause()
        await pilot.press("e")
        await pilot.pause()
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
        await pilot.press("j")  # descend onto first source
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


@pytest.mark.asyncio
async def test_a_adds_blank_source_row(cfg_three_collections: Config, tmp_index_dir: Path) -> None:
    app = AcornApp(index_dir=tmp_index_dir, config=cfg_three_collections)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("f3")
        await pilot.pause()
        # Cursor on coursework (collection). 'a' adds a source TO that
        # collection — works whether cursor is on the collection node or
        # one of its source children.
        await pilot.press("a")
        await pilot.pause()
        from textual.widgets import Input

        path_input = app.screen.query_one("#source_path_input", Input)
        assert path_input.value == ""


@pytest.mark.asyncio
async def test_x_removes_focused_source(cfg_three_collections: Config, tmp_index_dir: Path) -> None:
    app = AcornApp(index_dir=tmp_index_dir, config=cfg_three_collections)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("f3")
        await pilot.pause()
        # Descend to coursework's first source, then 'x' to remove it.
        await pilot.press("j")
        await pilot.pause()
        await pilot.press("x")
        await pilot.pause()
        screen = app.screen
        c = screen._config.collections["coursework"]  # type: ignore[attr-defined]
        assert len(c.sources) == 1  # was 2, now 1


@pytest.mark.asyncio
async def test_pasted_frontmatter_match_indicator(
    cfg_three_collections: Config, tmp_index_dir: Path
) -> None:
    app = AcornApp(index_dir=tmp_index_dir, config=cfg_three_collections)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("f3")
        await pilot.pause()
        await pilot.press("j")  # descend onto first source
        await pilot.pause()
        await pilot.press("e")
        await pilot.pause()
        from textual.widgets import Input, TextArea

        # Type a valid filter.
        filter_input = app.screen.query_one("#source_filter_input", Input)
        filter_input.value = "Course == 'DPwC'"
        await pilot.pause()

        # Paste matching frontmatter.
        sample = app.screen.query_one("#frontmatter_sample", TextArea)
        sample.text = "---\nCourse: DPwC\n---\n"
        await pilot.pause()

        match = app.screen.query_one("#frontmatter_match_status", Static)
        match_text = str(match.content)
        assert "match" in match_text.lower()
        assert "✓" in match_text

        # Now non-matching frontmatter.
        sample.text = "---\nCourse: Other\n---\n"
        await pilot.pause()
        match = app.screen.query_one("#frontmatter_match_status", Static)
        assert "no match" in str(match.content).lower() or "✗" in str(match.content)


@pytest.mark.asyncio
async def test_save_triggers_reindex_when_paths_change(
    tmp_path: Path,
    tmp_index_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A save that changes a source's path / includes / filter should
    auto-reindex the collection so the index reflects the new scope."""
    notes_a = tmp_path / "a"
    notes_b = tmp_path / "b"
    notes_a.mkdir()
    notes_b.mkdir()
    (notes_a / "x.md").write_text("# x\nblue penguin\n", encoding="utf-8")
    (notes_b / "y.md").write_text("# y\nblue penguin\n", encoding="utf-8")

    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        textwrap.dedent(f"""
            [[collections.notes.sources]]
            path = "{notes_a}"
            includes = ["**/*.md"]
        """),
        encoding="utf-8",
    )
    monkeypatch.setattr("acorn.config.default_config_path", lambda: cfg_path)
    cfg = load(cfg_path)

    # Build the initial index so the form will detect a "change" on save.
    from acorn.index import build_index_from_config

    build_index_from_config(
        config=cfg.collection("notes"), collection="notes", index_dir=tmp_index_dir
    )

    app = AcornApp(index_dir=tmp_index_dir, config=cfg)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("f3")
        await pilot.pause()
        # Descend onto the only source, then 'e' to edit.
        await pilot.press("j")
        await pilot.pause()
        await pilot.press("e")
        await pilot.pause()
        from textual.widgets import Input

        path_input = app.screen.query_one("#source_path_input", Input)
        path_input.value = str(notes_b)
        await pilot.press("ctrl+s")
        await pilot.pause()
        # Save the collection (triggers reindex because path changed).
        await pilot.press("s")
        await pilot.pause()

    # Searcher should now find y.md (notes_b) but not x.md (notes_a).
    from acorn.query import Searcher

    s = Searcher(index_dir=tmp_index_dir)
    paths = {Path(h.path).name for h in s.search("blue penguin", limit=10, collection="notes")}
    assert "y.md" in paths
    assert "x.md" not in paths


@pytest.mark.asyncio
async def test_s_persists_changes_to_config_toml(
    tmp_path: Path,
    tmp_index_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        textwrap.dedent("""
            # user comment
            [[collections.notes.sources]]
            path = "/tmp/old"
            includes = ["**/*.md"]
        """),
        encoding="utf-8",
    )
    monkeypatch.setattr("acorn.config.default_config_path", lambda: cfg_path)
    cfg = load(cfg_path)
    app = AcornApp(index_dir=tmp_index_dir, config=cfg)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("f3")
        await pilot.pause()
        # In the form, focus the first source and "remove" it (so the diff
        # is non-trivial: 1 source becomes 0). Default cursor lands on the
        # collection — press 'j' once to descend to its source child.
        await pilot.press("j")
        await pilot.pause()
        await pilot.press("x")
        await pilot.pause()
        # Save.
        await pilot.press("s")
        await pilot.pause()
    # File should reflect 0 sources for notes; comment preserved.
    text = cfg_path.read_text(encoding="utf-8")
    assert "# user comment" in text
    out = load(cfg_path)
    assert len(out.collection("notes").sources) == 0


@pytest.mark.asyncio
async def test_d_deletes_with_confirmation(
    cfg_three_collections: Config, tmp_index_dir: Path
) -> None:
    app = AcornApp(index_dir=tmp_index_dir, config=cfg_three_collections)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("f3")
        await pilot.pause()
        # Default selection: coursework. Press 'd' to delete.
        await pilot.press("d")
        await pilot.pause()
        # Confirmation modal asks "Delete 'coursework'? [y/N]". Press y.
        await pilot.press("y")
        await pilot.pause()
        screen = app.screen
        assert "coursework" not in screen._config.collections  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_n_creates_new_empty_collection(
    cfg_three_collections: Config, tmp_index_dir: Path
) -> None:
    app = AcornApp(index_dir=tmp_index_dir, config=cfg_three_collections)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("f3")
        await pilot.pause()
        await pilot.press("n")
        await pilot.pause()
        from textual.widgets import Input

        # New-name prompt mounts a single input.
        name_input = app.screen.query_one("#new_collection_name", Input)
        name_input.value = "research"
        await pilot.press("enter")
        await pilot.pause()
        screen = app.screen
        assert "research" in screen._config.collections  # type: ignore[attr-defined]
        # And research is now the selected collection.
        assert screen._selected == "research"  # type: ignore[attr-defined]


def test_strip_wrapping_quotes_helper() -> None:
    """The path field accepts paste-with-quotes — pure helper covers
    every shape the user might paste."""
    from acorn.tui.collections_screen import _strip_wrapping_quotes

    assert _strip_wrapping_quotes("/Users/x/y") == "/Users/x/y"
    assert _strip_wrapping_quotes("'/Users/x/y'") == "/Users/x/y"
    assert _strip_wrapping_quotes('"/Users/x/y"') == "/Users/x/y"
    # Whitespace around the wrapping quotes survives the strip.
    assert _strip_wrapping_quotes("  '/Users/x/y'  ") == "/Users/x/y"
    # Single quote at one end only — leave alone, probably a typo we
    # shouldn't silently swallow.
    assert _strip_wrapping_quotes("'/Users/x/y") == "'/Users/x/y"
    assert _strip_wrapping_quotes("/Users/x/y'") == "/Users/x/y'"
    # Empty / whitespace-only.
    assert _strip_wrapping_quotes("") == ""
    assert _strip_wrapping_quotes("   ") == ""


@pytest.mark.asyncio
async def test_source_edit_strips_wrapping_quotes_from_path(
    tmp_path: Path,
    tmp_index_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pasting ``'/path'`` (quoted) into the path Input must save as
    ``/path`` — without this, the walker looks for a directory whose name
    starts with a literal quote and silently indexes 0 chunks."""
    real_dir = tmp_path / "vault"
    real_dir.mkdir()
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        textwrap.dedent("""
            [[collections.notes.sources]]
            path = "/tmp/old"
        """),
        encoding="utf-8",
    )
    monkeypatch.setattr("acorn.config.default_config_path", lambda: cfg_path)
    cfg = load(cfg_path)

    app = AcornApp(index_dir=tmp_index_dir, config=cfg)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("f3")
        await pilot.pause()
        await pilot.press("j")  # descend onto first source
        await pilot.pause()
        await pilot.press("e")
        await pilot.pause()

        from textual.widgets import Input

        path_input = app.screen.query_one("#source_path_input", Input)
        path_input.value = f"'{real_dir}'"  # paste-with-single-quotes
        await pilot.press("ctrl+s")
        await pilot.pause()

        screen = app.screen
        # Modal dismissed; we're back on CollectionsScreen.
        c = screen._config.collections["notes"]  # type: ignore[attr-defined]
        # Path was de-quoted before being saved into SourceConfig.
        assert str(c.sources[0].path) == str(real_dir)


@pytest.mark.asyncio
async def test_source_edit_refuses_save_when_path_does_not_exist(
    tmp_path: Path,
    tmp_index_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-existent path is almost certainly a paste error or typo;
    refuse the save and leave the modal open so the user can fix it."""
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        textwrap.dedent("""
            [[collections.notes.sources]]
            path = "/tmp/old"
        """),
        encoding="utf-8",
    )
    monkeypatch.setattr("acorn.config.default_config_path", lambda: cfg_path)
    cfg = load(cfg_path)

    app = AcornApp(index_dir=tmp_index_dir, config=cfg)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("f3")
        await pilot.pause()
        await pilot.press("j")  # descend onto first source
        await pilot.pause()
        await pilot.press("e")
        await pilot.pause()

        from textual.widgets import Input

        path_input = app.screen.query_one("#source_path_input", Input)
        path_input.value = str(tmp_path / "absent" / "missing-dir")
        await pilot.press("ctrl+s")
        await pilot.pause()

        # Modal should still be open — the path Input is still queryable.
        assert app.screen.query("#source_path_input")
        # And the underlying source path on the parent screen is unchanged.
        # Reach into the underlying CollectionsScreen via the screen stack.
        for s in app.screen_stack:
            if hasattr(s, "_config"):
                c = s._config.collections["notes"]  # type: ignore[attr-defined]
                assert str(c.sources[0].path) == "/tmp/old"
                break
