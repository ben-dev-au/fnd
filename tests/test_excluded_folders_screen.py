"""Excluded folders on the per-source Index filters screen: a tree, typed paths, and a save."""

from __future__ import annotations

import dataclasses
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from textual.widgets import TextArea

from fnd.config import load
from fnd.tui import FNDApp


@pytest.fixture
def config_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from fnd.config import CollectionConfig, SourceConfig, write_collection

    cfg_path = tmp_path / "config.toml"
    for name in ("archive/old", "archive/keep", "projects", "notes"):
        (tmp_path / "src" / name).mkdir(parents=True)
    monkeypatch.setattr("fnd.config.default_config_path", lambda: cfg_path)
    write_collection(
        config_path=cfg_path,
        name="notes",
        collection=CollectionConfig(sources=[SourceConfig(path=tmp_path / "src")]),
    )
    return cfg_path


async def _settle(pilot: Any, n: int = 15) -> None:
    for _ in range(n):
        await pilot.pause()


def _nodes(node: Any) -> Iterator[Any]:
    for child in node.children:
        yield child
        yield from _nodes(child)


async def _source_filters(app: FNDApp, pilot: Any) -> Any:
    from fnd.tui.settings_screen import SourceFormScreen

    app.push_screen(SourceFormScreen(collection_name="notes", source_index=0))
    await _settle(pilot)
    form: Any = app.screen
    form._open_filters()
    await _settle(pilot, 5)
    browser: Any = app.screen
    for _ in range(300):
        if browser._dirs is not None:
            break
        await pilot.pause()
    await _settle(pilot, 5)
    return browser


@pytest.mark.asyncio
async def test_the_source_screen_lists_its_folders(config_file: Path, tmp_index_dir: Path) -> None:
    """The branch fills from the worker with the source's real sub-folders."""
    app = FNDApp(index_dir=tmp_index_dir, config=load(config_file))
    async with app.run_test(size=(120, 40)) as pilot:
        await _settle(pilot, 5)
        browser = await _source_filters(app, pilot)
        groups = browser.query_one("#filter_tree")._by_id.values()
        ids = {g.id for g in groups}
        leaves = {it.id for g in groups for it in g.items}
    assert "dir:archive" in ids
    assert {"dir:projects", "dir:notes", "dir:archive/old"} <= leaves


@pytest.mark.asyncio
async def test_a_ticked_folder_saves_on_the_source(config_file: Path, tmp_index_dir: Path) -> None:
    """Ticking in the tree, leaving and saving the form writes exclude_dirs on the source."""
    app = FNDApp(index_dir=tmp_index_dir, config=load(config_file))
    async with app.run_test(size=(120, 40)) as pilot:
        await _settle(pilot, 5)
        browser = await _source_filters(app, pilot)
        tree = browser.query_one("#filter_tree")
        tree._toggle(
            next(n for n in _nodes(tree.root) if (n.data or {}).get("id") == "dir:archive")
        )
        await _settle(pilot)
        await pilot.press("escape")
        await _settle(pilot)
        await pilot.press("ctrl+s")
        await _settle(pilot)
    cfg = load(config_file)
    source = cfg.collections["notes"].sources[0]
    assert source.exclude_dirs == ["archive"]
    assert source.excludes == cfg.defaults.filters.excludes


@pytest.mark.asyncio
async def test_typed_paths_hold_on_a_bad_one_and_carry_back_a_good_set(
    config_file: Path, tmp_index_dir: Path
) -> None:
    """The typed editor is a part: a bad line holds the user; valid lines hand back folded."""
    from fnd.tui.settings_screen import FolderTextScreen

    app = FNDApp(index_dir=tmp_index_dir, config=load(config_file))
    got: list[tuple[str, ...]] = []
    async with app.run_test(size=(120, 34)) as pilot:
        await _settle(pilot, 5)
        app.push_screen(
            FolderTextScreen(
                title="Excluded folders",
                value=(),
                on_save=got.append,
                root=config_file.parent / "src",
            )
        )
        await _settle(pilot)
        editor = app.screen
        box = editor.query_one("#glob_text", TextArea)
        box.text = "archive\n../escape"
        await _settle(pilot, 4)
        await pilot.press("escape")
        await _settle(pilot, 4)
        held = app.screen is editor
        box.text = "archive/\n./archive/old\nprojects"
        await _settle(pilot, 4)
        await pilot.press("escape")
        await _settle(pilot, 4)
    assert held
    assert got == [("archive", "projects")]


@pytest.mark.asyncio
async def test_the_status_names_a_path_that_is_not_there(
    config_file: Path, tmp_index_dir: Path
) -> None:
    """A typed folder missing from the source (or differing in case) is accepted but flagged."""
    from fnd.tui.settings_screen import FolderTextScreen

    app = FNDApp(index_dir=tmp_index_dir, config=load(config_file))
    async with app.run_test(size=(120, 34)) as pilot:
        await _settle(pilot, 5)
        app.push_screen(
            FolderTextScreen(
                title="Excluded folders",
                value=(),
                on_save=lambda _v: None,
                root=config_file.parent / "src",
            )
        )
        await _settle(pilot)
        app.screen.query_one("#glob_text", TextArea).text = "Archive\nprojects"
        await _settle(pilot, 4)
        text = str(app.screen.query_one("#glob_status").render())
    assert "1 not found" in text


@pytest.mark.asyncio
async def test_the_text_view_keeps_the_folders(config_file: Path, tmp_index_dir: Path) -> None:
    """Editing the expression as text never drops the excluded folders."""
    app = FNDApp(index_dir=tmp_index_dir, config=load(config_file))
    async with app.run_test(size=(120, 40)) as pilot:
        await _settle(pilot, 5)
        browser = await _source_filters(app, pilot)
        browser._spec = dataclasses.replace(browser._spec, exclude_dirs=("archive",))
        browser.action_edit_text()
        await _settle(pilot)
        app.screen.query_one("#filter_text", TextArea).text = "file.size <= 1000"
        await _settle(pilot, 4)
        await pilot.press("escape")
        await _settle(pilot)
        kept = browser._spec.exclude_dirs
    assert kept == ("archive",)


@pytest.mark.asyncio
async def test_the_global_screen_offers_no_folders_and_never_writes_the_key(
    config_file: Path, tmp_index_dir: Path
) -> None:
    """A folder path means nothing across sources, so the defaults screen omits it entirely."""
    from fnd.tui.menu import _open_filter_browser

    app = FNDApp(index_dir=tmp_index_dir, config=load(config_file))
    async with app.run_test(size=(120, 34)) as pilot:
        await _settle(pilot, 5)
        _open_filter_browser(app)
        await _settle(pilot)
        defaults_tree: Any = app.screen.query_one("#filter_tree")
        ids = [g.id for g in defaults_tree._by_id.values()]
        await pilot.press("ctrl+s")
        await _settle(pilot)
    assert "folders" not in ids
    assert "exclude_dirs" not in config_file.read_text(encoding="utf-8")


async def _folder_editor(app: FNDApp, pilot: Any, root: Path, value: tuple[str, ...] = ()) -> Any:
    from fnd.tui.settings_screen import FolderTextScreen

    got: list[tuple[str, ...]] = []
    app.push_screen(
        FolderTextScreen(title="Excluded folders", value=value, on_save=got.append, root=root)
    )
    await _settle(pilot)
    app.screen.got = got  # type: ignore[attr-defined]
    return app.screen


@pytest.mark.asyncio
async def test_a_comma_in_a_folder_name_is_part_of_the_name(
    config_file: Path, tmp_index_dir: Path
) -> None:
    """Folders are one per line: `Smith, Jane` is one folder, shown and saved as typed."""
    app = FNDApp(index_dir=tmp_index_dir, config=load(config_file))
    async with app.run_test(size=(120, 34)) as pilot:
        await _settle(pilot, 5)
        editor = await _folder_editor(app, pilot, config_file.parent / "src")
        editor.query_one("#glob_text", TextArea).text = "Clients/Smith, Jane\n  archive  "
        await _settle(pilot, 4)
        await pilot.press("escape")
        await _settle(pilot, 4)
        got = editor.got
        again = await _folder_editor(app, pilot, config_file.parent / "src", got[0])
        shown = again.query_one("#glob_text", TextArea).text
    assert got == [("Clients/Smith, Jane", "archive")]
    assert shown == "Clients/Smith, Jane\narchive"


@pytest.mark.asyncio
async def test_each_typed_folder_is_looked_up_once_not_per_keystroke(
    config_file: Path, tmp_index_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The existence check opens directories, so a path already checked is remembered."""
    from fnd.filters import dir_tree

    calls: list[str] = []
    real = dir_tree.exists_exactly
    monkeypatch.setattr(
        dir_tree, "exists_exactly", lambda root, rel: (calls.append(rel), real(root, rel))[1]
    )
    app = FNDApp(index_dir=tmp_index_dir, config=load(config_file))
    async with app.run_test(size=(120, 34)) as pilot:
        await _settle(pilot, 5)
        editor = await _folder_editor(app, pilot, config_file.parent / "src")
        box = editor.query_one("#glob_text", TextArea)
        for text in ("archive", "archive\nnotes", "archive\nnotes\nprojects", "archive\nnotes"):
            box.text = text
            await _settle(pilot, 3)
    assert sorted(calls) == ["archive", "notes", "projects"]


@pytest.mark.asyncio
async def test_a_row_filter_on_the_screen_keeps_the_hidden_siblings_excluded(
    config_file: Path, tmp_index_dir: Path
) -> None:
    """With the filter showing only `keep`, unticking it leaves `archive/old` excluded."""
    app = FNDApp(index_dir=tmp_index_dir, config=load(config_file))
    async with app.run_test(size=(120, 40)) as pilot:
        await _settle(pilot, 5)
        browser = await _source_filters(app, pilot)
        browser._spec = dataclasses.replace(browser._spec, exclude_dirs=("archive",))
        browser._query = "keep"
        browser._rebuild(focus_tree=False)
        await _settle(pilot)
        tree = browser.query_one("#filter_tree")
        tree._toggle(
            next(n for n in _nodes(tree.root) if (n.data or {}).get("id") == "dir:archive/keep")
        )
        await _settle(pilot)
        kept = browser._spec.exclude_dirs
    assert kept == ("archive/old",)
