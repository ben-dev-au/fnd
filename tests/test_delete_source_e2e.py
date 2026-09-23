"""End-to-end Pilot coverage for Ctrl+D source delete.

Earlier unit tests cover ``write_collection`` after a ``del col.sources[i]``
and that :class:`DeleteSourceScreen` imports. This file exercises the
real UI binding: open the SourceFormScreen, press Ctrl+D, walk the
confirm modal, confirm the on-disk TOML loses the row.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from textual.widgets import OptionList, Static

from fnd.config import (
    CollectionConfig,
    SourceConfig,
    load,
    write_collection,
)
from fnd.index import build_index
from fnd.tui import FNDApp
from fnd.tui.indexer_service import IndexerService
from fnd.tui.settings_screen import (
    DeleteSourceScreen,
    SourceFormScreen,
)


@pytest.fixture
def built_index(fixtures_dir: Path, tmp_index_dir: Path) -> Path:
    build_index(roots=[fixtures_dir], index_dir=tmp_index_dir, collection="default")
    return tmp_index_dir


@pytest.mark.asyncio
async def test_ctrl_d_in_source_form_pushes_delete_modal(
    built_index: Path,
    fixtures_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg_path = tmp_path / "config.toml"
    monkeypatch.setattr("fnd.config.default_config_path", lambda: cfg_path)
    extra = tmp_path / "extra"
    extra.mkdir()
    write_collection(
        config_path=cfg_path,
        name="probe",
        collection=CollectionConfig(
            sources=[
                SourceConfig(path=fixtures_dir),
                SourceConfig(path=extra),
            ]
        ),
    )

    app = FNDApp(index_dir=built_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._config = load()
        app.push_screen(SourceFormScreen(collection_name="probe", source_index=1))
        await pilot.pause()
        assert isinstance(app.screen, SourceFormScreen)
        await pilot.press("ctrl+d")
        await pilot.pause()
        assert isinstance(app.screen, DeleteSourceScreen)


@pytest.mark.asyncio
async def test_delete_modal_confirm_removes_source_and_lands_above(
    built_index: Path,
    fixtures_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After confirming, both the DeleteSourceScreen and the now-stale
    SourceFormScreen pop — the user lands on the screen they were on
    before opening the form (not the main app)."""
    cfg_path = tmp_path / "config.toml"
    monkeypatch.setattr("fnd.config.default_config_path", lambda: cfg_path)
    extra = tmp_path / "extra"
    extra.mkdir()
    write_collection(
        config_path=cfg_path,
        name="probe",
        collection=CollectionConfig(
            sources=[
                SourceConfig(path=fixtures_dir),
                SourceConfig(path=extra),
            ]
        ),
    )

    app = FNDApp(index_dir=built_index)
    # Suppress the reindex side-effect so the test stays hermetic.
    # Wizards now route through _reindex_with_warning_if_needed so the
    # user sees the IndexerScreen; stub it to a no-op for this test.
    monkeypatch.setattr(
        IndexerService,
        "reindex_with_warning",
        lambda self, name, **kwargs: None,
    )

    async with app.run_test() as pilot:
        await pilot.pause()
        app._config = load()
        sentinel = app.screen  # main screen — pop target after delete
        app.push_screen(SourceFormScreen(collection_name="probe", source_index=1))
        await pilot.pause()
        await pilot.press("ctrl+d")
        await pilot.pause()
        modal = app.screen
        assert isinstance(modal, DeleteSourceScreen)
        options = app.screen.query_one("#confirm_list", OptionList)
        options.highlighted = next(i for i, o in enumerate(options._options) if o.id == "yes")
        await pilot.press("enter")
        await pilot.pause()

        # Both DeleteSourceScreen and SourceFormScreen popped.
        assert app.screen is sentinel

        # TOML reflects the removal.
        reloaded = load()
        sources = reloaded.collections["probe"].sources
        assert len(sources) == 1
        # The remaining source is the first one, by path.
        assert Path(sources[0].path) == fixtures_dir


@pytest.mark.asyncio
async def test_delete_modal_cancel_keeps_source(
    built_index: Path,
    fixtures_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg_path = tmp_path / "config.toml"
    monkeypatch.setattr("fnd.config.default_config_path", lambda: cfg_path)
    extra = tmp_path / "extra"
    extra.mkdir()
    write_collection(
        config_path=cfg_path,
        name="probe",
        collection=CollectionConfig(
            sources=[
                SourceConfig(path=fixtures_dir),
                SourceConfig(path=extra),
            ]
        ),
    )

    app = FNDApp(index_dir=built_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._config = load()
        app.push_screen(SourceFormScreen(collection_name="probe", source_index=1))
        await pilot.pause()
        await pilot.press("ctrl+d")
        await pilot.pause()
        options = app.screen.query_one("#confirm_list", OptionList)
        options.highlighted = next(i for i, o in enumerate(options._options) if o.id == "no")
        await pilot.press("enter")
        await pilot.pause()
        # Back to SourceFormScreen, TOML unchanged.
        assert isinstance(app.screen, SourceFormScreen)
        reloaded = load()
        assert len(reloaded.collections["probe"].sources) == 2


class TestTheDialogDescribesWhatItDoes:
    """It promised orphans "until the next reindex" while _on_select runs
    reindex_with_warning(rebuild=True) immediately, so the promise held only
    in the case where the reindex fails to start. The rebuild is the whole
    collection, and the sibling delete-collection dialog warns about cost
    while this one said nothing."""

    @pytest.mark.asyncio
    async def test_the_text_names_the_rebuild_and_not_orphans(
        self,
        built_index: Path,
        fixtures_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        cfg_path = tmp_path / "config.toml"
        monkeypatch.setattr("fnd.config.default_config_path", lambda: cfg_path)
        extra = tmp_path / "extra"
        extra.mkdir()
        write_collection(
            config_path=cfg_path,
            name="probe",
            collection=CollectionConfig(
                sources=[SourceConfig(path=fixtures_dir), SourceConfig(path=extra)]
            ),
        )

        app = FNDApp(index_dir=built_index)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app._config = load()
            app.push_screen(DeleteSourceScreen(collection_name="probe", source_index=1))
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, DeleteSourceScreen)
            said = str(screen.query_one(".warning", Static).render())

        assert "orphaned" not in said, said
        assert "rebuilt" in said
        assert "files on disk are untouched" in said
        assert "another source still reaches" in said

    def test_the_confirm_still_rebuilds(self) -> None:
        """Guard: the text is pinned against the code it describes."""
        import inspect

        source = inspect.getsource(DeleteSourceScreen._on_select)
        assert "rebuild=True" in source
