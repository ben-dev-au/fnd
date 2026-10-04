"""The one act that empties an index asks before doing it.

Delete-source, delete-collection and Update-all all confirm. `Rebuild index`
sits one row under `Update index`, on a panel titled `Update index › <name>`,
differing from its neighbour only in cost and consequence: the update row gives
`0 newly / 71 already`, the rebuild row `72 newly / 0 already`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fnd.config import CollectionConfig, Config, SourceConfig
from fnd.tui import FNDApp
from fnd.tui.freshness_view import run_pending
from fnd.tui.menu import _make_rebuild
from fnd.tui.settings_screen import RebuildConfirmScreen


@pytest.fixture
def sandboxed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    """A config on disk, with `default_config_path` pointed at it.

    `RenameCollectionScreen._save` writes through that function, so a test
    without this would edit the developer's real configuration.
    """
    import textwrap

    from fnd.config import load

    root = tmp_path / "notes"
    root.mkdir()
    (root / "a.md").write_text("saffron\n", encoding="utf-8")
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        textwrap.dedent(f"""
            [[collections.papers.sources]]
            path = "{root.as_posix()}"
        """),
        encoding="utf-8",
    )
    monkeypatch.setattr("fnd.config.default_config_path", lambda: cfg_path)
    return load(cfg_path)


@pytest.fixture
def config(tmp_path: Path) -> Config:
    root = tmp_path / "notes"
    root.mkdir()
    (root / "a.md").write_text("saffron\n", encoding="utf-8")
    return Config(collections={"papers": CollectionConfig(sources=[SourceConfig(path=root)])})


@pytest.mark.asyncio
async def test_rebuild_asks_before_emptying(config: Config, tmp_index_dir: Path) -> None:
    started: list[str] = []
    app = FNDApp(index_dir=tmp_index_dir, config=config)
    async with app.run_test(size=(120, 34)) as pilot:
        await pilot.pause()
        app._indexer.reindex_with_warning = lambda name, **_kw: started.append(name)  # type: ignore[assignment]
        _make_rebuild("papers")(app)
        for _ in range(10):
            await pilot.pause()
        asked = app.screen.__class__ is RebuildConfirmScreen
        on_screen = "\n".join(
            "".join(s.text for s in strip) for strip in app.screen._compositor.render_strips()
        )

    assert asked, "the rebuild ran with no confirmation"
    assert not started, "it started the run before asking"
    assert "Rebuild 'papers' from scratch?" in on_screen, on_screen[:400]


@pytest.mark.asyncio
async def test_it_says_what_makes_it_different_from_update(
    config: Config, tmp_index_dir: Path
) -> None:
    """The two rows sit together and a label cannot carry the difference."""
    app = FNDApp(index_dir=tmp_index_dir, config=config)
    async with app.run_test(size=(120, 34)) as pilot:
        await pilot.pause()
        _make_rebuild("papers")(app)
        for _ in range(10):
            await pilot.pause()
        on_screen = "\n".join(
            "".join(s.text for s in strip) for strip in app.screen._compositor.render_strips()
        )

    assert "dropped first" in on_screen, on_screen[:400]
    assert "part-built" in on_screen, "it does not say what an interruption leaves"
    assert "Update index" in on_screen, "it does not name the cheaper act"


@pytest.mark.asyncio
async def test_confirming_starts_it(config: Config, tmp_index_dir: Path) -> None:
    from textual.widgets import OptionList

    started: list[str] = []
    app = FNDApp(index_dir=tmp_index_dir, config=config)
    async with app.run_test(size=(120, 34)) as pilot:
        await pilot.pause()
        app._indexer.reindex_with_warning = lambda name, **_kw: started.append(name)  # type: ignore[assignment]
        _make_rebuild("papers")(app)
        for _ in range(10):
            await pilot.pause()
        options = app.screen.query_one("#confirm_list", OptionList)
        options.highlighted = next(i for i, o in enumerate(options._options) if o.id == "yes")
        await pilot.pause()
        options.action_select()
        for _ in range(10):
            await pilot.pause()

    assert started == ["papers"], started


@pytest.mark.asyncio
async def test_cancelling_does_not(config: Config, tmp_index_dir: Path) -> None:
    from textual.widgets import OptionList

    started: list[str] = []
    app = FNDApp(index_dir=tmp_index_dir, config=config)
    async with app.run_test(size=(120, 34)) as pilot:
        await pilot.pause()
        app._indexer.reindex_with_warning = lambda name, **_kw: started.append(name)  # type: ignore[assignment]
        _make_rebuild("papers")(app)
        for _ in range(10):
            await pilot.pause()
        options = app.screen.query_one("#confirm_list", OptionList)
        options.highlighted = next(i for i, o in enumerate(options._options) if o.id == "no")
        await pilot.pause()
        options.action_select()
        for _ in range(10):
            await pilot.pause()
        gone = app.screen.__class__ is not RebuildConfirmScreen

    assert not started, "Cancel started the rebuild"
    assert gone


@pytest.mark.asyncio
async def test_update_index_still_runs_straight_away(config: Config, tmp_index_dir: Path) -> None:
    """The control: the cheap pass adds and drops what changed without
    emptying anything, so putting a dialog in front of it would be noise."""
    started: list[str] = []
    app = FNDApp(index_dir=tmp_index_dir, config=config)
    async with app.run_test(size=(120, 34)) as pilot:
        await pilot.pause()
        app._indexer.reindex_with_warning = lambda name, **_kw: started.append(name)  # type: ignore[assignment]
        run_pending(app, "papers")
        for _ in range(10):
            await pilot.pause()
        asked = app.screen.__class__ is RebuildConfirmScreen

    assert started == ["papers"], started
    assert not asked


def test_one_confirm_screen_serves_both_rebuilds() -> None:
    """The menu's rebuild and the out-of-date rebuild share one dialog class."""
    import inspect

    from fnd.tui import freshness_view, menu, settings_screen

    assert inspect.getsource(settings_screen).count("class RebuildConfirmScreen") == 1
    assert "RebuildConfirmScreen(" in inspect.getsource(menu)
    assert "RebuildConfirmScreen(" in inspect.getsource(freshness_view)


class TestTheDeclineSaysWhatItDeclines:
    """The cursor starts on the safe option, as on the unsaved-changes gate."""

    @pytest.mark.asyncio
    async def test_the_rebuild_dialog_defaults_to_safe_too(
        self, config: Config, tmp_index_dir: Path
    ) -> None:
        """The same screen serves both acts, so both land safely."""
        from textual.widgets import OptionList

        app = FNDApp(index_dir=tmp_index_dir, config=config)
        async with app.run_test(size=(120, 34)) as pilot:
            await pilot.pause()
            _make_rebuild("papers")(app)
            for _ in range(10):
                await pilot.pause()
            options = app.screen.query_one("#confirm_list", OptionList)
            landed = options._options[options.highlighted or 0].id

        assert landed == "no", "Enter on arrival would have emptied the collection"


@pytest.mark.asyncio
@pytest.mark.parametrize("texturise", [None, True])
async def test_update_all_and_rebuild_all_land_on_cancel(
    config: Config, tmp_index_dir: Path, texturise: bool | None
) -> None:
    """Every confirm opens on the row that changes nothing."""
    from textual.widgets import OptionList

    from fnd.tui.menu import _push_update_all_confirm

    app = FNDApp(index_dir=tmp_index_dir, config=config)
    async with app.run_test(size=(120, 34)) as pilot:
        await pilot.pause()
        _push_update_all_confirm(app, texturise_override=texturise)
        for _ in range(10):
            await pilot.pause()
        options = app.screen.query_one("#confirm_list", OptionList)
        landed = options._options[options.highlighted or 0].id
    assert landed == "no", landed
