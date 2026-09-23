"""A settings screen open when a run finishes repaints to the finished status.

Backgrounding the indexer modal resumes the settings screen straight away, so
`on_screen_resume` repaints it against a run still in flight. Without a repaint
on finish, `⚠ nothing indexed` sat over a collection its own modal reported
Done 12/12, and `⚠ incomplete` for 45 seconds against a 5/5 index.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from fnd.config import Config, load
from fnd.index import build_index
from fnd.tui import FNDApp
from fnd.tui.menu import SECTION_COLLECTIONS
from fnd.tui.settings_screen import SettingsScreen, open_settings_section
from tests._pilot_wait import settings_ready


@pytest.fixture
def config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    root = tmp_path / "vault"
    root.mkdir()
    (root / "a.md").write_text("saffron\n", encoding="utf-8")
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        textwrap.dedent(f"""
            [[collections.notes.sources]]
            path = "{root.as_posix()}"
        """),
        encoding="utf-8",
    )
    monkeypatch.setattr("fnd.config.default_config_path", lambda: cfg_path)
    return load(cfg_path)


def _row(app: FNDApp, label: str) -> str:
    """The PAINTED row. Calling the value_getter reads config fresh every time
    and can never show a stale screen, which is the whole question here."""
    screen = app.screen
    assert isinstance(screen, SettingsScreen)
    rows = ["".join(s.text for s in strip) for strip in screen._compositor.render_strips()]
    return next((r for r in rows if label in r), "")


@pytest.mark.asyncio
async def test_the_row_clears_when_the_run_it_described_finishes(
    config: Config, tmp_path: Path, tmp_index_dir: Path
) -> None:
    # The index must EXIST and hold nothing for this collection: with no index
    # at all the badge stays silent on purpose, that being first run.
    other = tmp_path / "other"
    other.mkdir()
    (other / "b.md").write_text("stock\n", encoding="utf-8")
    build_index(roots=[other], index_dir=tmp_index_dir, collection="other")

    app = FNDApp(index_dir=tmp_index_dir, config=config)
    async with app.run_test(size=(110, 32)) as pilot:
        await pilot.pause()
        open_settings_section(app, SECTION_COLLECTIONS)
        await settings_ready(pilot, app)
        assert "nothing indexed" in _row(app, "notes"), "precondition: notes holds nothing"

        # What a finished run leaves behind, then the hook the modal calls.
        build_index(roots=[tmp_path / "vault"], index_dir=tmp_index_dir, collection="notes")
        app._indexer.on_reindex_complete()
        for _ in range(10):
            await pilot.pause()
        after = _row(app, "notes")

    assert "nothing indexed" not in after, after


@pytest.mark.asyncio
async def test_a_row_with_nothing_to_correct_is_left_alone(
    config: Config, tmp_path: Path, tmp_index_dir: Path
) -> None:
    """The control: the repaint must not invent a badge on a healthy row."""
    build_index(roots=[tmp_path / "vault"], index_dir=tmp_index_dir, collection="notes")
    app = FNDApp(index_dir=tmp_index_dir, config=config)
    async with app.run_test(size=(110, 32)) as pilot:
        await pilot.pause()
        open_settings_section(app, SECTION_COLLECTIONS)
        await settings_ready(pilot, app)
        app._indexer.on_reindex_complete()
        for _ in range(10):
            await pilot.pause()
        after = _row(app, "notes")

    assert "nothing indexed" not in after, after
    assert "1 source" in after, after
