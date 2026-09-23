"""A no-op source-form save is a no-op, on every field the form cannot show.

``action_save_close`` rebuilt a fresh SourceConfig from the form's own fields,
so opening a source, changing nothing and pressing Ctrl+S deleted ``app_for``
and every ``app_params`` key but ``vault``. ``app_for[kind]`` is the first step
of app resolution, so afterwards ``o`` opened files with the wrong app and the
form had no row to show what was lost.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fnd.config import (
    CollectionConfig,
    SourceConfig,
    SourceFilters,
    load,
    write_collection,
)
from fnd.index import build_index
from fnd.tui import FNDApp
from fnd.tui.settings_screen import SourceFormScreen
from tests._pilot_wait import screen_ready

# Rewritten by design, with the reason. Everything else must survive.
_REWRITTEN = {"frontmatter_filter": "absorbed into filters.frontmatter on save"}


@pytest.fixture
def built_index(fixtures_dir: Path, tmp_index_dir: Path) -> Path:
    build_index(roots=[fixtures_dir], index_dir=tmp_index_dir, collection="default")
    return tmp_index_dir


def _source(root: Path) -> SourceConfig:
    return SourceConfig(
        path=root,
        includes=["notes/**"],
        excludes=["**/scratch/**"],
        follow_symlinks=True,
        filters=SourceFilters(kinds=["md"], min_size=10),
        app="vscode",
        app_for={"md": "obsidian", "pdf": "preview"},
        app_params={"vault": "MyVault", "profile": "work"},
    )


async def _save_without_editing(app: FNDApp, pilot: object, cfg_path: Path) -> SourceConfig:
    app._config = load()
    app.push_screen(SourceFormScreen(collection_name="probe", source_index=0))
    await pilot.pause()  # type: ignore[attr-defined]
    assert isinstance(app.screen, SourceFormScreen)
    await pilot.press("ctrl+s")  # type: ignore[attr-defined]
    await pilot.pause()  # type: ignore[attr-defined]
    return load(cfg_path).collections["probe"].sources[0]


@pytest.mark.asyncio
async def test_saving_an_unedited_source_keeps_app_routing(
    built_index: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_path = tmp_path / "config.toml"
    monkeypatch.setattr("fnd.config.default_config_path", lambda: cfg_path)
    root = tmp_path / "vault"
    root.mkdir()
    write_collection(
        config_path=cfg_path, name="probe", collection=CollectionConfig(sources=[_source(root)])
    )

    app = FNDApp(index_dir=built_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        saved = await _save_without_editing(app, pilot, cfg_path)

    assert saved.app_for == {"md": "obsidian", "pdf": "preview"}
    assert saved.app_params == {"vault": "MyVault", "profile": "work"}


@pytest.mark.asyncio
async def test_saving_an_unedited_source_keeps_every_field(
    built_index: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Enumerated, so a field added later is covered without editing this."""
    cfg_path = tmp_path / "config.toml"
    monkeypatch.setattr("fnd.config.default_config_path", lambda: cfg_path)
    root = tmp_path / "vault"
    root.mkdir()
    before = _source(root)
    write_collection(
        config_path=cfg_path, name="probe", collection=CollectionConfig(sources=[before])
    )

    app = FNDApp(index_dir=built_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        after = await _save_without_editing(app, pilot, cfg_path)

    lost = {
        name: (getattr(before, name), getattr(after, name))
        for name in SourceConfig.model_fields
        if name not in _REWRITTEN and getattr(before, name) != getattr(after, name)
    }
    assert not lost, f"a no-op save changed {lost}"


@pytest.mark.asyncio
async def test_clearing_the_vault_leaves_the_other_params(
    built_index: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one app_params key the form owns is the only one it may remove."""
    cfg_path = tmp_path / "config.toml"
    monkeypatch.setattr("fnd.config.default_config_path", lambda: cfg_path)
    root = tmp_path / "vault"
    root.mkdir()
    write_collection(
        config_path=cfg_path, name="probe", collection=CollectionConfig(sources=[_source(root)])
    )

    app = FNDApp(index_dir=built_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._config = load()
        app.push_screen(SourceFormScreen(collection_name="probe", source_index=0))
        await screen_ready(pilot, app, SourceFormScreen)
        form = app.screen
        assert isinstance(form, SourceFormScreen)
        form._fields["app_params_vault"] = ""
        await pilot.press("ctrl+s")
        await pilot.pause()
        saved = load(cfg_path).collections["probe"].sources[0]

    assert saved.app_params == {"profile": "work"}
