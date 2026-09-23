"""The one act that empties an index asks before doing it.

Delete-source, delete-collection and Update-all all confirm. `Rebuild index`
sits one row under `Update index`, on a panel titled `Update index › <name>`,
differing from its neighbour only in cost and consequence: the update row gives
`0 newly / 71 already`, the rebuild row `72 newly / 0 already`.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from textual.app import ComposeResult
from textual.screen import Screen
from textual.widgets import Static

from fnd.config import CollectionConfig, Config, SourceConfig
from fnd.tui import FNDApp
from fnd.tui.menu import _make_rebuild, _make_reindex
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
        _make_reindex("papers")(app)
        for _ in range(10):
            await pilot.pause()
        asked = app.screen.__class__ is RebuildConfirmScreen

    assert started == ["papers"], started
    assert not asked


class TestARenameAsksBeforeDroppingTheIndex:
    """A rename dropped the old name's documents and rebuilt from scratch on
    the Enter that submitted the text field: minutes of work on a large
    collection, from typing a name.

    The config write is not the part being confirmed: that is saved either
    way. What is confirmed is emptying the index.
    """

    @staticmethod
    async def _rename_to(app: FNDApp, pilot: object, new_name: str, dropped: list[str]) -> None:
        """Drive the real submit path, with the config path already redirected
        by the fixture: `_save` writes through `default_config_path()`."""
        from textual.widgets import Input

        from fnd.tui.settings_screen import RenameCollectionScreen

        # `_save` pops twice (past Rename and the now-stale per-collection
        # screen), so the stack has to be as deep as the real one.
        app.push_screen(_Filler())
        for _ in range(4):
            await pilot.pause()  # type: ignore[attr-defined]
        screen = RenameCollectionScreen(collection_name="papers")
        app.push_screen(screen)
        for _ in range(10):
            await pilot.pause()  # type: ignore[attr-defined]
        screen._drop_old_then_reindex = lambda _app, name: dropped.append(name)  # type: ignore[assignment]
        field = screen.query_one(Input)
        field.value = new_name
        screen._save(Input.Submitted(field, new_name))
        for _ in range(12):
            await pilot.pause()  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_it_asks(self, sandboxed: Config, tmp_index_dir: Path) -> None:
        dropped: list[str] = []
        app = FNDApp(index_dir=tmp_index_dir, config=sandboxed)
        async with app.run_test(size=(120, 34)) as pilot:
            await pilot.pause()
            await self._rename_to(app, pilot, "papers2", dropped)
            asked = app.screen.__class__ is RebuildConfirmScreen
            on_screen = "\n".join(
                "".join(s.text for s in strip) for strip in app.screen._compositor.render_strips()
            )

        assert asked, "it dropped the index straight from the text field"
        assert not dropped, "it started before asking"
        assert "Reindex it now?" in on_screen, on_screen[:400]
        assert "config is already saved" in on_screen, "it did not say what was already done"

    @pytest.mark.asyncio
    async def test_declining_leaves_the_index_alone(
        self, sandboxed: Config, tmp_index_dir: Path
    ) -> None:
        from textual.widgets import OptionList

        dropped: list[str] = []
        app = FNDApp(index_dir=tmp_index_dir, config=sandboxed)
        async with app.run_test(size=(120, 34)) as pilot:
            await pilot.pause()
            await self._rename_to(app, pilot, "papers2", dropped)
            options = app.screen.query_one("#confirm_list", OptionList)
            options.highlighted = next(i for i, o in enumerate(options._options) if o.id == "no")
            await pilot.pause()
            options.action_select()
            for _ in range(10):
                await pilot.pause()

        assert not dropped, "Cancel dropped the index anyway"

    @pytest.mark.asyncio
    async def test_confirming_does_the_work(self, sandboxed: Config, tmp_index_dir: Path) -> None:
        from textual.widgets import OptionList

        dropped: list[str] = []
        app = FNDApp(index_dir=tmp_index_dir, config=sandboxed)
        async with app.run_test(size=(120, 34)) as pilot:
            await pilot.pause()
            await self._rename_to(app, pilot, "papers2", dropped)
            options = app.screen.query_one("#confirm_list", OptionList)
            options.highlighted = next(i for i, o in enumerate(options._options) if o.id == "yes")
            await pilot.pause()
            options.action_select()
            for _ in range(10):
                await pilot.pause()

        assert dropped == ["papers2"], dropped


def test_one_confirm_screen_serves_both_acts() -> None:
    """A second dialog class would be a second thing to keep in step."""
    import inspect

    from fnd.tui import settings_screen

    source = inspect.getsource(settings_screen)
    assert source.count("class RebuildConfirmScreen") == 1
    assert source.count("RebuildConfirmScreen(") >= 2, "the rename grew its own dialog"


class _Filler(Screen[None]):
    """Stands in for the per-collection screen the rename pops past."""

    def compose(self) -> ComposeResult:
        yield Static("collection")


class TestTheDeclineSaysWhatItDeclines:
    """ "Cancel" on the rename dialog cancelled nothing: the rename is written
    before the dialog appears, and only the reindex is on offer. A user
    reading "Cancel" reasonably expects the rename undone.

    And the cursor starts on the safe option, as on the unsaved-changes gate.
    """

    @pytest.mark.asyncio
    async def test_the_rename_decline_does_not_say_cancel(
        self, sandboxed: Config, tmp_index_dir: Path
    ) -> None:
        app = FNDApp(index_dir=tmp_index_dir, config=sandboxed)
        async with app.run_test(size=(120, 34)) as pilot:
            await pilot.pause()
            await TestARenameAsksBeforeDroppingTheIndex._rename_to(app, pilot, "papers2", [])
            on_screen = "\n".join(
                "".join(s.text for s in strip) for strip in app.screen._compositor.render_strips()
            )

        assert "leave the index for now" in on_screen, on_screen[:500]
        assert "Cancel" not in on_screen, "it still offers to cancel what is already done"

    @pytest.mark.asyncio
    async def test_the_cursor_starts_on_the_safe_option(
        self, sandboxed: Config, tmp_index_dir: Path
    ) -> None:
        from textual.widgets import OptionList

        app = FNDApp(index_dir=tmp_index_dir, config=sandboxed)
        async with app.run_test(size=(120, 34)) as pilot:
            await pilot.pause()
            await TestARenameAsksBeforeDroppingTheIndex._rename_to(app, pilot, "papers2", [])
            options = app.screen.query_one("#confirm_list", OptionList)
            landed = options._options[options.highlighted or 0].id

        assert landed == "no", "Enter on arrival would have started the rebuild"

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
