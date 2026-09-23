"""Three surfaces advertised keys that do nothing, or offered nothing at all.

`/` focuses a row filter, and the screens without one (the source form, the
wizard, every confirm dialog) named it anyway. The indexer modal is a modal
screen, so the app's own footer showed through it: four anchors, none of which
work while it is up, and none of the keys that do. And the ranking picker
opened on an empty list, because `[ranking.*]` blocks are optional while the
row it edits always holds a value.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from fnd.config import CollectionConfig, Config, SourceConfig, load, write_collection
from fnd.tui import FNDApp
from fnd.tui.indexer_modal import IndexerScreen
from fnd.tui.menu import _choices_ranking
from fnd.tui.settings_screen import SourceFormScreen


def _footer(app: FNDApp) -> str:
    rows = ["".join(s.text for s in strip) for strip in app.screen._compositor.render_strips()]
    return rows[-1]


@pytest.fixture
def config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    cfg_path = tmp_path / "config.toml"
    root = tmp_path / "vault"
    root.mkdir()
    monkeypatch.setattr("fnd.config.default_config_path", lambda: cfg_path)
    write_collection(
        config_path=cfg_path,
        name="probe",
        collection=CollectionConfig(sources=[SourceConfig(path=root)]),
    )
    return load(cfg_path)


@pytest.mark.asyncio
async def test_a_form_with_no_row_filter_does_not_offer_one(
    config: Config, tmp_index_dir: Path
) -> None:
    app = FNDApp(index_dir=tmp_index_dir, config=config)
    async with app.run_test(size=(110, 30)) as pilot:
        await pilot.pause()
        app.push_screen(SourceFormScreen(collection_name="probe", source_index=0))
        for _ in range(25):
            await pilot.pause()
        footer = _footer(app)
        before = (type(app.focused).__name__, len(app.screen_stack))
        await pilot.press("slash")
        for _ in range(5):
            await pilot.pause()
        after = (type(app.focused).__name__, len(app.screen_stack))

    assert before == after, "the key must still do nothing: that is the point"
    assert "Search" not in footer, footer
    assert "Menu" in footer, "the anchors that do work stay"


@pytest.mark.asyncio
async def test_a_screen_with_a_row_filter_names_it_once(
    config: Config, tmp_index_dir: Path
) -> None:
    """One key, one label. The anchor means "focus the query bar", which `/`
    never does in Settings: on a screen with a row filter it focuses THAT, so
    the screen's own cluster is where the key is named. Keeping the anchor
    there would show `/ Search` and `/ Filter` together."""
    from fnd.tui.settings_screen import open_settings

    app = FNDApp(index_dir=tmp_index_dir, config=config)
    async with app.run_test(size=(110, 30)) as pilot:
        await pilot.pause()
        await pilot.press("escape")
        open_settings(app)
        for _ in range(20):
            await pilot.pause()
        # A focused text box makes every anchor inert, and the footer already
        # drops all four there; this is about the screen, not the focus.
        from fnd.tui.settings_screen import SettingsList

        app.screen.query_one(SettingsList).focus()
        for _ in range(5):
            await pilot.pause()
        footer = _footer(app)

    assert "Filter" in footer, "the screen that owns the key must still name it"
    assert "Search" not in footer, f"one key, two labels: {footer}"


@pytest.mark.asyncio
async def test_the_indexer_modal_names_its_own_keys(config: Config, tmp_index_dir: Path) -> None:
    app = FNDApp(index_dir=tmp_index_dir, config=config)
    async with app.run_test(size=(110, 30)) as pilot:
        await pilot.pause()
        app.push_screen(IndexerScreen("probe"))
        for _ in range(15):
            await pilot.pause()
        footer = _footer(app)

    assert "Background" in footer, footer
    assert "Cancel" in footer, footer
    for dead in ("Search", "Menu", "Keys", "Quit"):
        assert dead not in footer, f"{dead} does nothing while the modal is up: {footer}"


def test_the_ranking_picker_offers_the_profile_the_row_holds() -> None:
    """`[ranking.*]` is optional; `ranking_profile` is not."""
    from fnd.config import DEFAULT_RANKING_PROFILE

    empty = cast("Any", SimpleNamespace(_config=Config()))
    assert [c.value for c in _choices_ranking(empty)] == [DEFAULT_RANKING_PROFILE]


def test_a_configured_profile_is_offered_beside_it(tmp_path: Path) -> None:
    from fnd.config import DEFAULT_RANKING_PROFILE

    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        textwrap.dedent("""
            [ranking.recent]
            recency_boost = 1.0
        """),
        encoding="utf-8",
    )
    app = cast("Any", SimpleNamespace(_config=load(cfg_path)))
    offered = [c.value for c in _choices_ranking(app)]

    assert "recent" in offered
    assert DEFAULT_RANKING_PROFILE in offered, "the fallback must not vanish once one is defined"


def test_a_source_that_carries_no_tags_is_flagged_as_such() -> None:
    """`sample_source` seeds `tags` with an empty dict per provider, so the
    mapping is truthy on a source with none and the note never fired."""
    from fnd.filters.scan import SourceSample
    from fnd.tui.settings_screen import _any_tag

    seeded = SourceSample(tags={"frontmatter": {}, "os": {}})

    assert not _any_tag(seeded), "an empty dict per provider is not a tag"
    assert not _any_tag(None)
    assert _any_tag(SourceSample(tags={"frontmatter": {"keep": 1}}))
