"""Removing a source acts on the file as it is now, and on the source shown.

`DeleteSourceScreen` deleted by row index from `app._config`, a model loaded at
launch and replaced whenever an index run or a settings write reloads it, then
wrote the collection back wholesale. A source added from a shell was written
away, one removed by hand came back, and a reload underneath moved the index
onto a different source. The delete also edited `app._config` in place before
the write, so a failed write left the form behind it pointing past the end:
`Path: (unknown)`, a false "only source" line, and a Yes that could not work.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any

import pytest
from textual.screen import Screen
from textual.widgets import OptionList, Static

from fnd.config import load
from fnd.tui import FNDApp
from fnd.tui.indexer_service import IndexerService
from fnd.tui.settings_screen import DeleteSourceScreen, SourceFormScreen
from tests._pilot_wait import wait_until


@pytest.fixture
def cfg_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for name in ("one", "two", "three"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "a.md").write_text("saffron\n", encoding="utf-8")
    path = tmp_path / "config.toml"
    monkeypatch.setattr("fnd.config.default_config_path", lambda: path)
    monkeypatch.setattr(IndexerService, "reindex_with_warning", lambda self, name, **kw: None)
    _write(path, tmp_path, "one", "two")
    return path


def _write(cfg_path: Path, root: Path, *names: str) -> None:
    cfg_path.write_text(
        "".join(
            textwrap.dedent(f"""
                [[collections.notes.sources]]
                path = "{(root / name).as_posix()}"
            """)
            for name in names
        ),
        encoding="utf-8",
    )


def _names(cfg_path: Path) -> list[str]:
    return [Path(s.path).name for s in load(cfg_path).collections["notes"].sources]


async def _open_form(app: FNDApp, pilot: Any, index: int) -> None:
    app._config = load()
    app.push_screen(Screen())
    app.push_screen(SourceFormScreen(collection_name="notes", source_index=index))
    await wait_until(pilot, lambda: isinstance(app.screen, SourceFormScreen))
    for _ in range(5):
        await pilot.pause()


async def _open_dialog(app: FNDApp, pilot: Any) -> tuple[str, list[str | None]]:
    await pilot.press("ctrl+d")
    await wait_until(
        pilot,
        lambda: (
            isinstance(app.screen, DeleteSourceScreen) and bool(app.screen.query("#confirm_list"))
        ),
    )
    for _ in range(3):
        await pilot.pause()
    body = " ".join(" ".join(str(w.content) for w in app.screen.query(Static)).split())
    options = app.screen.query_one("#confirm_list", OptionList)
    return body, [o.id for o in options._options]


async def _choose_yes(app: FNDApp, pilot: Any) -> None:
    dialog = app.screen
    options = dialog.query_one("#confirm_list", OptionList)
    options.highlighted = options.get_option_index("yes")
    options.action_select()
    for _ in range(10):
        await pilot.pause()


@pytest.mark.asyncio
async def test_a_source_added_from_a_shell_survives(
    cfg_path: Path, tmp_path: Path, tmp_index_dir: Path
) -> None:
    """A source added to the file after launch is still there after deleting another."""
    app = FNDApp(index_dir=tmp_index_dir)
    async with app.run_test(size=(110, 34)) as pilot:
        await pilot.pause()
        await _open_form(app, pilot, 0)
        _write(cfg_path, tmp_path, "one", "two", "three")
        await _open_dialog(app, pilot)
        await _choose_yes(app, pilot)

    assert _names(cfg_path) == ["two", "three"]


@pytest.mark.asyncio
async def test_a_source_removed_by_hand_stays_removed(
    cfg_path: Path, tmp_path: Path, tmp_index_dir: Path
) -> None:
    """Deleting one source does not write back another the file no longer holds."""
    app = FNDApp(index_dir=tmp_index_dir)
    async with app.run_test(size=(110, 34)) as pilot:
        await pilot.pause()
        await _open_form(app, pilot, 1)
        _write(cfg_path, tmp_path, "two")
        await _open_dialog(app, pilot)
        await _choose_yes(app, pilot)

    assert _names(cfg_path) == []


@pytest.mark.asyncio
async def test_a_reload_underneath_does_not_move_the_target(
    cfg_path: Path, tmp_path: Path, tmp_index_dir: Path
) -> None:
    """The source removed is the one the form shows, wherever the file now puts it."""
    app = FNDApp(index_dir=tmp_index_dir)
    async with app.run_test(size=(110, 34)) as pilot:
        await pilot.pause()
        await _open_form(app, pilot, 0)
        _write(cfg_path, tmp_path, "two", "one")
        app._config = load()
        body, _ = await _open_dialog(app, pilot)
        await _choose_yes(app, pilot)

    assert _names(cfg_path) == ["two"]
    assert str(tmp_path / "one") in body, body


@pytest.mark.asyncio
async def test_a_source_gone_from_the_file_is_named_and_not_offered(
    cfg_path: Path, tmp_path: Path, tmp_index_dir: Path
) -> None:
    """A form whose source has left the file says so and offers no removal."""
    app = FNDApp(index_dir=tmp_index_dir)
    async with app.run_test(size=(110, 34)) as pilot:
        await pilot.pause()
        await _open_form(app, pilot, 1)
        _write(cfg_path, tmp_path, "one")
        app._config = load()
        body, choices = await _open_dialog(app, pilot)

    assert "(unknown)" not in body, body
    assert "only source" not in body, body
    assert str(tmp_path / "two") in body, body
    assert "yes" not in choices, choices
    assert _names(cfg_path) == ["one"]


@pytest.mark.asyncio
async def test_a_failed_delete_leaves_the_form_able_to_retry(
    cfg_path: Path, tmp_path: Path, tmp_index_dir: Path
) -> None:
    """A delete that could not be written leaves the form's source intact to retry."""
    app = FNDApp(index_dir=tmp_index_dir)
    async with app.run_test(size=(110, 34)) as pilot:
        await pilot.pause()
        await _open_form(app, pilot, 1)
        await _open_dialog(app, pilot)
        good = cfg_path.read_text(encoding="utf-8")
        cfg_path.write_text(good + "\n[[[ half-saved\n", encoding="utf-8")
        await _choose_yes(app, pilot)
        cfg_path.write_text(good, encoding="utf-8")
        if isinstance(app.screen, DeleteSourceScreen):
            app.pop_screen()
        await wait_until(pilot, lambda: isinstance(app.screen, SourceFormScreen))
        body, _ = await _open_dialog(app, pilot)
        await _choose_yes(app, pilot)

    assert "(unknown)" not in body, body
    assert "only source" not in body, body
    assert _names(cfg_path) == ["one"]
