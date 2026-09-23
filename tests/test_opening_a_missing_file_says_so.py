"""`o` and `Shift+R` on a file that is no longer there did nothing, silently.

Polled at 0.35, 0.85 and 1.9 seconds: byte-identical screens. The same screen
shows a good error for a malformed query, so silence reads as "it opened, in
another window". The index keeps serving the deleted file's stored body, so
the row and the preview look ordinary.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from textual.widgets import Tree

from fnd.config import Config, load
from fnd.index import build_index
from fnd.tui import FNDApp
from tests._pilot_wait import wait_until


@pytest.fixture
def indexed(tmp_path: Path, tmp_index_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    root = tmp_path / "notes"
    root.mkdir()
    (root / "vanish.md").write_text("# Vanish\n\nsnickersnee here.\n", encoding="utf-8")
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


async def _press_on_a_deleted_row(
    indexed: Config, tmp_path: Path, tmp_index_dir: Path, key: str, monkeypatch: pytest.MonkeyPatch
) -> tuple[list[str], list[Path]]:
    """Returns the notifications raised, and any path the opener was asked for."""
    asked: list[Path] = []
    for name in ("open_smart", "open_default", "reveal"):
        monkeypatch.setattr(
            f"fnd.opener.{name}",
            lambda *_a, path=None, **kw: asked.append(Path(path or kw.get("path", ""))),
            raising=False,
        )
    (tmp_path / "notes" / "vanish.md").unlink()

    app = FNDApp(
        index_dir=tmp_index_dir, config=indexed, collection="notes", initial_query="snickersnee"
    )
    said: list[str] = []
    async with app.run_test(size=(110, 30)) as pilot:
        await wait_until(
            pilot,
            lambda: bool(app.query_one("#results_pane", Tree).root.children),
            timeout=30.0,
            message="the search never produced a row",
        )
        tree = app.query_one("#results_pane", Tree)
        tree.focus()
        tree.cursor_line = 0
        monkeypatch.setattr(
            type(app), "notify", lambda _s, msg, **kw: said.append(str(msg)), raising=False
        )
        await pilot.press(key)
        for _ in range(6):
            await pilot.pause()
    return said, asked


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["o", "O", "R"])
async def test_a_missing_file_is_not_opened_in_silence(
    key: str, indexed: Config, tmp_path: Path, tmp_index_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`o` opens at the locator, `O` offers apps, `R` reveals. None of the
    three can work, and none of them may look like it did."""
    said, asked = await _press_on_a_deleted_row(indexed, tmp_path, tmp_index_dir, key, monkeypatch)

    assert said, "the keypress was silent"
    assert any("no longer on disk" in m for m in said), said
    assert not asked, asked
