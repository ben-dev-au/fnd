"""The sidebar read `1/1 active, 1/1 sources` for a collection pointing at a
folder that does not exist.

Reached through the app's own offered `Reset to defaults`, which points a
collection at `~/Documents`. The index kept serving that collection's stale
hits, so every surface agreed and none of them was right.

The Sources row inside Settings already says `⚠ path not found`. The sidebar,
which is where a user looks first and the only one visible while searching,
said nothing.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from textual.widgets import Tree

from fnd.config import Config, load
from fnd.tui import FNDApp
from tests._pilot_wait import wait_until


def _config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, present: bool) -> Config:
    root = tmp_path / "vault"
    if present:
        root.mkdir()
        (root / "a.md").write_text("# A\n\nrisotto.\n", encoding="utf-8")
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        textwrap.dedent(f"""
            [[collections.vault.sources]]
            path = "{root.as_posix()}"
        """),
        encoding="utf-8",
    )
    monkeypatch.setattr("fnd.config.default_config_path", lambda: cfg_path)
    return load(cfg_path)


async def _sidebar_rows(cfg: Config, index_dir: Path) -> list[str]:
    app = FNDApp(index_dir=index_dir, config=cfg, collection="vault")
    async with app.run_test(size=(110, 30)) as pilot:
        await wait_until(
            pilot,
            lambda: bool(app.query_one("#collections_panel_tree", Tree).root.children),
            timeout=30.0,
            message="the collections panel never populated",
        )
        tree = app.query_one("#collections_panel_tree", Tree)
        return [str(node.label) for node in tree.root.children]


@pytest.mark.asyncio
async def test_a_collection_whose_folder_is_gone_is_marked(
    tmp_path: Path, tmp_index_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It cannot index anything, and the row said only how many sources it had."""
    cfg = _config(tmp_path, monkeypatch, present=False)

    rows = await _sidebar_rows(cfg, tmp_index_dir)

    assert rows, rows
    assert any("⚠" in row for row in rows), rows


@pytest.mark.asyncio
async def test_a_healthy_collection_is_not_marked(
    tmp_path: Path, tmp_index_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control: a warning on every row is a warning nobody reads."""
    cfg = _config(tmp_path, monkeypatch, present=True)

    rows = await _sidebar_rows(cfg, tmp_index_dir)

    assert rows, rows
    assert not any("⚠" in row for row in rows), rows
