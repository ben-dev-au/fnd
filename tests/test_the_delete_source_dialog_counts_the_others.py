"""The delete-source dialog promised "Files another source still reaches stay"
to a collection that had no other source.

It is the reassuring half of the sentence, and on a one-source collection it is
false: everything that source reached leaves, and the collection is emptied.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from textual.widgets import Static

from fnd.config import Config, load
from fnd.tui import FNDApp
from fnd.tui.settings_screen import DeleteSourceScreen


def _config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, n: int) -> Config:
    lines = []
    for i in range(n):
        root = tmp_path / f"src{i}"
        root.mkdir()
        lines.append(f'[[collections.notes.sources]]\npath = "{root.as_posix()}"\n')
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(textwrap.dedent("".join(lines)), encoding="utf-8")
    monkeypatch.setattr("fnd.config.default_config_path", lambda: cfg_path)
    return load(cfg_path)


async def _body(app: FNDApp, pilot: object) -> str:
    app.push_screen(DeleteSourceScreen(collection_name="notes", source_index=0))
    for _ in range(15):
        await pilot.pause()  # type: ignore[attr-defined]
    return " ".join(" ".join(str(w.content) for w in app.screen.query(Static)).split())


@pytest.mark.asyncio
async def test_the_only_source_is_told_the_collection_empties(
    tmp_path: Path, tmp_index_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = FNDApp(index_dir=tmp_index_dir, config=_config(tmp_path, monkeypatch, 1))
    async with app.run_test(size=(120, 34)) as pilot:
        await pilot.pause()
        body = await _body(app, pilot)

    assert "only source" in body, body
    assert "another source still reaches" not in body, body


@pytest.mark.asyncio
async def test_a_shared_collection_keeps_the_reassurance(
    tmp_path: Path, tmp_index_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control: the sentence is true where there IS another source."""
    app = FNDApp(index_dir=tmp_index_dir, config=_config(tmp_path, monkeypatch, 2))
    async with app.run_test(size=(120, 34)) as pilot:
        await pilot.pause()
        body = await _body(app, pilot)

    assert "another source still reaches" in body, body
    assert "only source" not in body, body
