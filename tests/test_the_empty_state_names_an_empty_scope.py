"""The empty state explained the tag filters and never mentioned that no
collection was in scope.

Nothing ticked searches nothing, which is the emptier reason and sits above
the filters in the same sidebar.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from fnd.config import Config, load
from fnd.index import build_index
from fnd.tui import FNDApp


@pytest.fixture
def indexed(tmp_path: Path, tmp_index_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    root = tmp_path / "notes"
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
    build_index(roots=[root], index_dir=tmp_index_dir, collection="notes")
    return load(cfg_path)


@pytest.mark.asyncio
async def test_an_empty_scope_is_named(indexed: Config, tmp_index_dir: Path) -> None:
    app = FNDApp(index_dir=tmp_index_dir, config=indexed, collection="notes")
    async with app.run_test(size=(110, 30)) as pilot:
        for _ in range(15):
            await pilot.pause()
        app._scope.selection = {}
        app._search.current_query = "saffron"
        message = app._results.empty_state()

    assert "No collections are in scope" in message, message
    assert "Collections panel" in message, message


@pytest.mark.asyncio
async def test_a_scoped_search_does_not_say_it(indexed: Config, tmp_index_dir: Path) -> None:
    """The control: the line must not appear whenever a search finds nothing."""
    app = FNDApp(index_dir=tmp_index_dir, config=indexed, collection="notes")
    async with app.run_test(size=(110, 30)) as pilot:
        for _ in range(15):
            await pilot.pause()
        app._search.current_query = "zzzznothing"
        scoped = bool(app._scope.collections or app._scope.active_sources)
        message = app._results.empty_state()

    assert scoped, "the app should open with something in scope"
    assert "No collections are in scope" not in message, message
    assert "zzzznothing" in message, message
