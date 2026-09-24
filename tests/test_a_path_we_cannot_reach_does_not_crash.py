"""`Path.exists()` is not a total function, and two new call sites assumed it was.

It re-raises every OSError outside ENOENT/ENOTDIR/EBADF/ELOOP, so a path under
a directory with mode 000 raises EACCES. The sidebar's per-source check runs
from `on_mount`, so a locked volume in the config stopped the app from
launching at all; the same call in the open action took the app down on the
keypress that exists to explain itself.

Cannot-look answers the same as present. A warning nobody can act on is worse
than none, which is the rule `is_stale` already follows.
"""

from __future__ import annotations

import os
import stat as stat_mod
import textwrap
from pathlib import Path

import pytest
from textual.widgets import Tree

from fnd.config import Config, load
from fnd.fsmeta import path_is_absent
from fnd.index import build_index
from fnd.tui import FNDApp
from tests._pilot_wait import wait_until


def _locked(tmp_path: Path) -> Path:
    """A real file under a directory the process cannot search."""
    locked = tmp_path / "Volume"
    locked.mkdir()
    inner = locked / "Notes"
    inner.mkdir()
    (inner / "a.md").write_text("# A\n\nrisotto.\n", encoding="utf-8")
    os.chmod(locked, 0o000)
    if os.access(locked, os.R_OK):
        os.chmod(locked, stat_mod.S_IRWXU)
        pytest.skip("running as a user that bypasses directory permissions")
    return inner


def test_a_path_we_cannot_reach_is_not_called_absent(tmp_path: Path) -> None:
    inner = _locked(tmp_path)
    try:
        answer = path_is_absent(inner)
    finally:
        os.chmod(tmp_path / "Volume", stat_mod.S_IRWXU)

    assert answer is False


def test_a_path_that_is_really_gone_is_called_absent(tmp_path: Path) -> None:
    """The control: the check still has to be able to say yes."""
    assert path_is_absent(tmp_path / "never") is True
    (tmp_path / "here.md").write_text("x\n", encoding="utf-8")
    assert path_is_absent(tmp_path / "here.md") is False


@pytest.mark.asyncio
async def test_the_app_launches_with_a_source_it_cannot_reach(
    tmp_path: Path, tmp_index_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sidebar's per-source check runs from `on_mount`, so a raise there is fatal."""
    inner = _locked(tmp_path)
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        textwrap.dedent(f"""
            [[collections.vault.sources]]
            path = "{inner.as_posix()}"
        """),
        encoding="utf-8",
    )
    monkeypatch.setattr("fnd.config.default_config_path", lambda: cfg_path)
    cfg: Config = load(cfg_path)

    try:
        app = FNDApp(index_dir=tmp_index_dir, config=cfg, collection="vault")
        async with app.run_test(size=(100, 30)) as pilot:
            tree = app.query_one("#collections_panel_tree", Tree)
            await wait_until(
                pilot,
                lambda: bool(tree.root.children),
                timeout=30.0,
                message="the app never finished mounting",
            )
            rows = [str(node.label) for node in tree.root.children]
    finally:
        os.chmod(tmp_path / "Volume", stat_mod.S_IRWXU)

    assert rows, rows
    assert not any("⚠" in row for row in rows), rows


@pytest.mark.asyncio
async def test_pressing_open_on_a_file_we_cannot_reach_does_not_take_the_app_down(
    tmp_path: Path, tmp_index_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real keypress, because the crash is an exception escaping an ACTION.

    The file is still there and still indexed; only its directory is closed.
    """
    root = tmp_path / "Volume" / "Notes"
    root.mkdir(parents=True)
    (root / "vanish.md").write_text("# Vanish\n\nsnickersnee here.\n", encoding="utf-8")
    build_index(roots=[root], index_dir=tmp_index_dir, collection="notes")

    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        textwrap.dedent(f"""
            [[collections.notes.sources]]
            path = "{root.as_posix()}"
        """),
        encoding="utf-8",
    )
    monkeypatch.setattr("fnd.config.default_config_path", lambda: cfg_path)
    cfg: Config = load(cfg_path)

    asked: list[Path] = []
    for name in ("open_smart", "open_default", "reveal"):
        monkeypatch.setattr(
            f"fnd.opener.{name}",
            lambda *_a, path=None, **kw: asked.append(Path(path or kw.get("path", ""))),
            raising=False,
        )

    os.chmod(tmp_path / "Volume", 0o000)
    if os.access(tmp_path / "Volume", os.R_OK):
        os.chmod(tmp_path / "Volume", stat_mod.S_IRWXU)
        pytest.skip("running as a user that bypasses directory permissions")

    try:
        app = FNDApp(
            index_dir=tmp_index_dir,
            config=cfg,
            collection="notes",
            initial_query="snickersnee",
        )
        async with app.run_test(size=(110, 30)) as pilot:
            tree = app.query_one("#results_pane", Tree)
            await wait_until(
                pilot,
                lambda: bool(tree.root.children),
                timeout=30.0,
                message="the search never produced a row",
            )
            tree.focus()
            tree.cursor_line = 0
            await pilot.press("alt+o")
            for _ in range(6):
                await pilot.pause()
            alive = app.is_running
    finally:
        os.chmod(tmp_path / "Volume", stat_mod.S_IRWXU)

    assert alive, "the keypress took the app down"
    assert asked, "a file we cannot stat is still a file we should try to open"
