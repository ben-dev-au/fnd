"""A fresh install launches the TUI on Add Collection, and that collection is searched."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from fnd.cli import app as cli_app
from fnd.config import CollectionConfig, Config, SourceConfig, load
from fnd.index import build_index
from fnd.tui import FNDApp
from fnd.tui.indexer_service import IndexerService
from fnd.tui.settings_screen import AddCollectionWizard
from tests._pilot_wait import wait_until

runner = CliRunner()


def _collection(root: Path) -> CollectionConfig:
    root.mkdir(exist_ok=True)
    return CollectionConfig(sources=[SourceConfig(path=root)])


async def _launch_screen(app: FNDApp) -> Any:
    async with app.run_test(size=(120, 40)) as pilot:
        for _ in range(10):
            await pilot.pause()
        return app.screen


def test_the_tui_launches_with_no_index(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """End to end through the CLI guard: an absent index is a fresh install."""
    ran: list[FNDApp] = []
    monkeypatch.setattr("fnd.cli.default_index_dir", lambda: tmp_path / "idx")
    monkeypatch.setattr("fnd.extract._worker.warm_pool", lambda: None)
    monkeypatch.setattr(FNDApp, "run", lambda self, **_kw: ran.append(self))

    result = runner.invoke(cli_app, ["tui"])

    assert result.exit_code == 0, result.output
    assert len(ran) == 1


def test_search_still_refuses_with_no_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control: only the TUI can build an index for itself."""
    monkeypatch.setattr("fnd.cli.default_index_dir", lambda: tmp_path / "idx")

    result = runner.invoke(cli_app, ["search", "risotto"])

    assert result.exit_code == 1
    assert "no index at" in result.output


@pytest.mark.asyncio
async def test_no_collections_and_no_index_opens_the_wizard(tmp_index_dir: Path) -> None:
    screen = await _launch_screen(FNDApp(index_dir=tmp_index_dir, config=Config()))
    assert isinstance(screen, AddCollectionWizard)


@pytest.mark.asyncio
async def test_leaving_the_wizard_shows_the_query_bar_keys(tmp_index_dir: Path) -> None:
    """Back on the main screen, the footer names the focused query bar's keys."""
    from textual.widgets import Static

    app = FNDApp(index_dir=tmp_index_dir, config=Config())
    async with app.run_test(size=(120, 40)) as pilot:
        await wait_until(pilot, lambda: isinstance(app.screen, AddCollectionWizard), timeout=10.0)
        await pilot.press("escape")
        footer = app.query_one("#footer_hints", Static)
        await wait_until(
            pilot,
            lambda: app.screen is app.screen_stack[0] and "Run" in str(footer.content),
            timeout=10.0,
            message="the footer never showed the query bar's keys",
        )
        assert "Quit" not in str(footer.content), footer.content


@pytest.mark.asyncio
async def test_a_configured_collection_opens_the_main_screen(
    tmp_path: Path, tmp_index_dir: Path
) -> None:
    """The control: a collection not yet indexed is managed from the main screen."""
    cfg = Config(collections={"notes": _collection(tmp_path / "notes")})
    screen = await _launch_screen(FNDApp(index_dir=tmp_index_dir, config=cfg))
    assert not isinstance(screen, AddCollectionWizard)


@pytest.mark.asyncio
async def test_an_existing_index_opens_the_main_screen(tmp_path: Path, tmp_index_dir: Path) -> None:
    """The control: an index without a config is not a fresh install."""
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "a.md").write_text("# a\n\nrisotto\n", encoding="utf-8")
    build_index(roots=[tmp_path / "docs"], index_dir=tmp_index_dir, collection="docs")
    screen = await _launch_screen(FNDApp(index_dir=tmp_index_dir, config=Config()))
    assert not isinstance(screen, AddCollectionWizard)


async def _save_wizard(
    app: FNDApp, name: str, root: Path, monkeypatch: pytest.MonkeyPatch, *, untick: bool = False
) -> list[str]:
    monkeypatch.setattr(IndexerService, "reindex_with_warning", lambda *_a, **_k: None)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        if untick:
            app._scope.selection.clear()
        wizard = AddCollectionWizard()
        wizard._fields["name"] = name
        wizard._fields["path"] = str(root)
        app.push_screen(wizard)
        await pilot.pause()
        await pilot.press("ctrl+s")
        await wait_until(
            pilot,
            lambda: name in (app._config.collections if app._config else {}),
            timeout=10.0,
            message="the wizard never saved",
        )
        return list(app._scope.collections)


@pytest.mark.asyncio
async def test_the_first_collection_is_in_scope(
    tmp_path: Path, tmp_index_dir: Path, isolated_config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first collection is searched without a relaunch, and no scope is saved for it."""
    from fnd.state import load as load_state

    monkeypatch.setattr("fnd.config.default_config_path", lambda: isolated_config_path)
    (tmp_path / "recipes").mkdir()
    app = FNDApp(index_dir=tmp_index_dir, config=load(isolated_config_path))
    assert await _save_wizard(app, "recipes", tmp_path / "recipes", monkeypatch) == ["recipes"]
    assert not load_state().saved


@pytest.mark.asyncio
async def test_a_later_collection_leaves_an_emptied_scope_alone(
    tmp_path: Path, tmp_index_dir: Path, isolated_config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control: with another collection the empty scope is the user's choice."""
    from fnd.config import write_collection

    monkeypatch.setattr("fnd.config.default_config_path", lambda: isolated_config_path)
    write_collection(
        config_path=isolated_config_path, name="notes", collection=_collection(tmp_path / "notes")
    )
    (tmp_path / "recipes").mkdir()
    app = FNDApp(index_dir=tmp_index_dir, config=load(isolated_config_path))
    saved = await _save_wizard(app, "recipes", tmp_path / "recipes", monkeypatch, untick=True)
    assert saved == []


@pytest.mark.asyncio
async def test_a_first_reload_seeds_every_new_collection(
    tmp_path: Path, tmp_index_dir: Path
) -> None:
    """The config file route: `defaults.collection = all` ticks each collection it brought."""
    app = FNDApp(index_dir=tmp_index_dir, config=Config())
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app._config = Config(
            collections={n: _collection(tmp_path / n) for n in ("default", "recipes")}
        )
        app._scope.refresh_collections_panel()
        assert sorted(app._scope.collections) == ["default", "recipes"]


@pytest.mark.asyncio
async def test_an_index_built_outside_the_app_is_searched(
    tmp_path: Path, tmp_index_dir: Path
) -> None:
    """`fnd collection reindex` from another terminal, while the app runs with no index."""
    root = tmp_path / "docs"
    cfg = Config(collections={"docs": _collection(root)})
    (root / "a.md").write_text("# a\n\nrisotto here.\n", encoding="utf-8")
    app = FNDApp(index_dir=tmp_index_dir, config=cfg)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        assert app._search.searcher is None
        build_index(roots=[root], index_dir=tmp_index_dir, collection="docs")
        app._search.run("risotto")
        await wait_until(
            pilot,
            lambda: bool(app._search.groups),
            timeout=15.0,
            message="the search never found the index built outside the app",
        )
