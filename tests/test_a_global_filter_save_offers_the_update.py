"""Saving the global filters offers to update every collection.

`[defaults.filters]` govern all of them, and the defaults route deliberately
reindexes nothing, so without the offer every collection stays behind the
config (measured: config=3 against index=4, a just-excluded file searchable).
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from fnd.config import Config, load
from fnd.tui import FNDApp
from fnd.tui.settings_screen import FilterBrowserScreen, UpdateAllConfirm


@pytest.fixture
def three_collections(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    lines = []
    for name in ("alpha", "beta", "gamma"):
        root = tmp_path / name
        root.mkdir()
        (root / "note.md").write_text("saffron\n", encoding="utf-8")
        lines.append(f'[[collections.{name}.sources]]\npath = "{root}"\n')
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(textwrap.dedent("".join(lines)), encoding="utf-8")
    monkeypatch.setattr("fnd.config.default_config_path", lambda: cfg_path)
    return load(cfg_path)


async def _save_a_filter(app: FNDApp, pilot: object) -> None:
    from fnd.tui.menu import _open_filter_browser

    _open_filter_browser(app)
    for _ in range(25):
        await pilot.pause()  # type: ignore[attr-defined]
    browser = app.screen
    assert isinstance(browser, FilterBrowserScreen)
    browser._spec = browser._spec.__class__(max_size=1_000_000)
    browser.action_save_close()
    for _ in range(20):
        await pilot.pause()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_it_offers_to_update_every_collection(
    three_collections: Config, tmp_index_dir: Path
) -> None:
    app = FNDApp(index_dir=tmp_index_dir, config=three_collections)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await _save_a_filter(app, pilot)
        landed = app.screen

    assert isinstance(landed, UpdateAllConfirm), type(landed).__name__
    assert landed._names == ["alpha", "beta", "gamma"], "every collection is behind, not one"


@pytest.mark.asyncio
async def test_declining_leaves_the_save_in_place(
    three_collections: Config, tmp_index_dir: Path
) -> None:
    """The offer is an offer: Esc must not undo what was written."""
    from fnd.config import default_config_path

    app = FNDApp(index_dir=tmp_index_dir, config=three_collections)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await _save_a_filter(app, pilot)
        await pilot.press("escape")
        for _ in range(10):
            await pilot.pause()
        gone = not isinstance(app.screen, UpdateAllConfirm)
        saved = load(default_config_path()).defaults.filters.max_size

    assert gone
    assert saved == 1_000_000


@pytest.mark.asyncio
async def test_a_save_that_changes_nothing_offers_nothing(
    three_collections: Config, tmp_index_dir: Path
) -> None:
    """The control: an unchanged set never reaches the save at all, so it must
    not push a dialog asking to reindex a corpus that is already current."""
    from fnd.tui.menu import _open_filter_browser

    app = FNDApp(index_dir=tmp_index_dir, config=three_collections)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        _open_filter_browser(app)
        for _ in range(25):
            await pilot.pause()
        app.screen.action_save_close()  # type: ignore[attr-defined]
        for _ in range(20):
            await pilot.pause()
        landed = app.screen

    assert not isinstance(landed, UpdateAllConfirm), "nothing changed, nothing to update"
